"""
Blockchain address crawler + Arkham-filtered BFS path finder.

BFS search space reduction via Arkham label pre-filtering:
  - Each neighbor is queried against Arkham Intelligence before being enqueued
  - ~80% of neighbors (hot wallets, DEX pools, bridges) are pruned
  - Only individual wallets, CEX deposits, and unknowns are explored
"""

import time
import sqlite3
import requests
from collections import deque
from typing import Optional, Callable
from arkham import get_label_with_cache, classify_address

# ─── Chain API Config ─────────────────────────────────────────────────────────

EVM_APIS = {
    'ETH':   'https://api.etherscan.io/api',
    'BSC':   'https://api.bscscan.com/api',
    'ARB':   'https://api.arbiscan.io/api',
    'OP':    'https://api-optimistic.etherscan.io/api',
    'MATIC': 'https://api.polygonscan.com/api',
    'AVAX':  'https://api.snowtrace.io/api',
}

TRON_API      = 'https://apilist.tronscanapi.com/api'
REQUEST_DELAY = 0.22   # seconds between blockchain API calls
TX_LIMIT      = 100    # max txs fetched per address


# ─── Transaction Fetching ─────────────────────────────────────────────────────

def _fetch_evm_txs(address: str, api_base: str, api_key: str) -> list:
    params = {
        'module': 'account', 'action': 'txlist',
        'address': address,
        'startblock': 0, 'endblock': 99999999,
        'page': 1, 'offset': TX_LIMIT,
        'sort': 'desc',
        'apikey': api_key or 'YourApiKeyToken',
    }
    try:
        r = requests.get(api_base, params=params, timeout=12)
        data = r.json()
        return data.get('result', []) if data.get('status') == '1' else []
    except Exception:
        return []


def _fetch_tron_txs(address: str, api_key: str) -> list:
    try:
        params = {'address': address, 'limit': TX_LIMIT, 'sort': '-timestamp'}
        headers = {'TRON-PRO-API-KEY': api_key} if api_key else {}
        r = requests.get(f"{TRON_API}/transaction", params=params,
                         headers=headers, timeout=12)
        return r.json().get('data', [])
    except Exception:
        return []


def fetch_transactions(address: str, chain: str, api_key: str = '') -> list:
    chain = chain.upper()
    if chain == 'TRON':
        return _fetch_tron_txs(address, api_key)
    return _fetch_evm_txs(address, EVM_APIS.get(chain, EVM_APIS['ETH']), api_key)


def get_neighbors(address: str, chain: str, api_key: str = '') -> list:
    """
    Return list of {address, tx_hash, value, timestamp, direction}
    for all counterparties of `address`.
    """
    txs = fetch_transactions(address, chain, api_key)
    seen, neighbors = set(), []
    addr_lower = address.lower()
    chain = chain.upper()

    if chain == 'TRON':
        for tx in txs:
            src = (tx.get('ownerAddress') or '').lower()
            dst = (tx.get('toAddress') or '').lower()
            other = dst if src == addr_lower else src
            if other and other != addr_lower and other not in seen:
                seen.add(other)
                neighbors.append({
                    'address': other,
                    'tx_hash': tx.get('hash', ''),
                    'value': str(tx.get('amount', 0)),
                    'timestamp': str(tx.get('timestamp', '')),
                    'direction': 'out' if src == addr_lower else 'in',
                })
    else:
        for tx in txs:
            src = (tx.get('from') or '').lower()
            dst = (tx.get('to') or '').lower()
            other = dst if src == addr_lower else src
            if other and other != addr_lower and other not in seen:
                seen.add(other)
                try:
                    val_str = f"{int(tx.get('value', 0)) / 1e18:.6f}"
                except Exception:
                    val_str = tx.get('value', '0')
                neighbors.append({
                    'address': other,
                    'tx_hash': tx.get('hash', ''),
                    'value': val_str,
                    'timestamp': tx.get('timeStamp', ''),
                    'direction': 'out' if src == addr_lower else 'in',
                })
    return neighbors


# ─── Arkham-filtered BFS ──────────────────────────────────────────────────────

def find_path(
    addr_a: str,
    addr_b: str,
    chain: str = 'ETH',
    chain_api_key: str = '',
    arkham_api_key: str = '',
    db_path: str = 'fund_network.db',
    max_depth: int = 3,
    progress_cb: Optional[Callable] = None,
) -> Optional[dict]:
    """
    BFS with Arkham label pre-filtering.

    At each hop, every neighbor address is checked against Arkham:
      - skip=True  (hot wallet, pool, bridge, etc.) → pruned, not enqueued
      - skip=False (individual, deposit, unknown)    → enqueued for exploration

    Returns:
      {
        'path': [hop, ...],          # list of hops with arkham metadata
        'stats': {                    # pruning statistics
          'total_neighbors': int,
          'filtered': int,
          'explored': int,
          'filter_rate': float,
        }
      }
    or None if not found.
    """
    addr_a = addr_a.strip().lower()
    addr_b = addr_b.strip().lower()

    def prog(msg: str):
        if progress_cb:
            progress_cb(msg)

    # Stats accumulator
    stats = {'total_neighbors': 0, 'filtered': 0, 'explored': 0}

    if addr_a == addr_b:
        return {'path': [_start_hop(addr_a)], 'stats': stats}

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    queue = deque()
    queue.append((addr_a, [_start_hop(addr_a)]))
    visited = {addr_a}

    try:
        while queue:
            current, path = queue.popleft()
            depth = len(path) - 1

            if depth >= max_depth:
                continue

            prog(f"[深度 {depth+1}/{max_depth}] 分析 {_short(current)}")
            time.sleep(REQUEST_DELAY)

            neighbors = get_neighbors(current, chain, chain_api_key)
            total = len(neighbors)
            stats['total_neighbors'] += total

            kept, pruned = [], []

            for nbr in neighbors:
                naddr = nbr['address']

                # Query Arkham (uses SQLite cache)
                label_info = get_label_with_cache(naddr, arkham_api_key, db)
                nbr['arkham'] = label_info

                if label_info['skip']:
                    pruned.append(f"{_short(naddr)} [{label_info['reason']}]")
                else:
                    kept.append(nbr)

            filtered = len(pruned)
            stats['filtered'] += filtered
            stats['explored'] += len(kept)
            rate = filtered / total * 100 if total else 0

            prog(f"  {total} 个对手方 → 过滤 {filtered} 个（{rate:.0f}%），保留 {len(kept)} 个继续溯源")

            for nbr in kept[:3]:  # Log first few kept addresses
                lbl = nbr['arkham'].get('label') or '未知'
                prog(f"  ✓ {_short(nbr['address'])} [{lbl}]")
            if len(kept) > 3:
                prog(f"  … 还有 {len(kept)-3} 个")

            for nbr in neighbors:
                if nbr['address'] == addr_b:
                    new_path = path + [nbr]
                    stats_summary = {
                        **stats,
                        'filter_rate': stats['filtered'] / max(stats['total_neighbors'], 1)
                    }
                    prog(f"✅ 找到路径！{len(new_path)-1} 跳，"
                         f"总过滤率 {stats_summary['filter_rate']*100:.0f}%")
                    db.close()
                    return {'path': new_path, 'stats': stats_summary}

            for nbr in kept:
                naddr = nbr['address']
                if naddr not in visited:
                    visited.add(naddr)
                    queue.append((naddr, path + [nbr]))

        prog(f"❌ 未找到路径（已探索 {stats['explored']} 个节点，"
             f"总过滤率 {stats['filtered']/max(stats['total_neighbors'],1)*100:.0f}%）")
        db.close()
        return None

    except Exception as e:
        db.close()
        raise e


# ─── Batch Analysis ───────────────────────────────────────────────────────────

def analyze_addresses(
    addresses: list,
    chain: str = 'ETH',
    chain_api_key: str = '',
    arkham_api_key: str = '',
    db_path: str = 'fund_network.db',
    progress_cb: Optional[Callable] = None,
) -> dict:
    """
    Fetch and label all counterparties for a list of seed addresses.
    Arkham-filters neighbors so only meaningful nodes enter the graph.

    Returns:
      {
        'input_addresses': [...],
        'nodes': [{'address', 'label', 'entity', 'skip', ...}, ...],
        'edges': [{'from', 'to', 'tx_hash', 'value', 'timestamp'}, ...],
        'paths': {'A:B': [hop, ...]},    # direct connections found
        'stats': { total_neighbors, filtered, explored, filter_rate }
      }
    """
    def prog(msg):
        if progress_cb:
            progress_cb(msg)

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    addr_set = {a.strip().lower() for a in addresses if a.strip()}
    all_nodes = {}   # addr -> node_dict
    all_edges = []
    seen_edges = set()
    stats = {'total_neighbors': 0, 'filtered': 0, 'explored': 0}

    try:
        for addr in addr_set:
            prog(f"获取 {_short(addr)} 的交易记录…")
            time.sleep(REQUEST_DELAY)
            neighbors = get_neighbors(addr, chain, chain_api_key)
            total = len(neighbors)
            stats['total_neighbors'] += total

            # Label the address itself
            self_label = get_label_with_cache(addr, arkham_api_key, db)
            all_nodes[addr] = {**self_label, 'address': addr, 'is_input': True}

            kept = 0
            for nbr in neighbors:
                naddr = nbr['address']
                label_info = get_label_with_cache(naddr, arkham_api_key, db)

                if label_info['skip']:
                    stats['filtered'] += 1
                    continue

                stats['explored'] += 1
                kept += 1

                if naddr not in all_nodes:
                    all_nodes[naddr] = {**label_info, 'address': naddr, 'is_input': False}

                edge_key = tuple(sorted([addr, naddr]))
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    all_edges.append({
                        'from': addr, 'to': naddr,
                        'tx_hash': nbr['tx_hash'],
                        'value': nbr['value'],
                        'timestamp': nbr['timestamp'],
                    })

            rate = (total - kept) / total * 100 if total else 0
            prog(f"  {total} 个对手方，过滤 {total-kept} 个（{rate:.0f}%），保留 {kept} 个")

        # Find direct connections between input addresses
        input_addrs = list(addr_set)
        paths = {}
        for i in range(len(input_addrs)):
            for j in range(i + 1, len(input_addrs)):
                a, b = input_addrs[i], input_addrs[j]
                direct = [e for e in all_edges
                          if (e['from'] == a and e['to'] == b) or
                             (e['from'] == b and e['to'] == a)]
                if direct:
                    paths[f"{a}:{b}"] = [
                        _start_hop(a),
                        {**direct[0], 'address': b, 'direction': 'direct'},
                    ]
                    prog(f"🔗 直接关联: {_short(a)} ↔ {_short(b)}")

        db.close()
        return {
            'input_addresses': list(addr_set),
            'nodes': list(all_nodes.values()),
            'edges': all_edges,
            'paths': paths,
            'stats': {
                **stats,
                'filter_rate': stats['filtered'] / max(stats['total_neighbors'], 1),
            },
        }

    except Exception as e:
        db.close()
        raise e


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _short(addr: str) -> str:
    return addr[:10] + '…' if len(addr) > 12 else addr


def _start_hop(addr: str) -> dict:
    return {'address': addr, 'tx_hash': '', 'value': '0',
            'timestamp': '', 'direction': 'start', 'arkham': {}}
