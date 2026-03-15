"""
Blockchain address crawler + BFS path finder.
Supports EVM chains (Etherscan-compatible APIs) and TRON.
"""

import time
import requests
from collections import deque
from typing import Optional

# ─── Chain API Config ─────────────────────────────────────────────────────────

EVM_APIS = {
    'ETH':   'https://api.etherscan.io/api',
    'BSC':   'https://api.bscscan.com/api',
    'ARB':   'https://api.arbiscan.io/api',
    'OP':    'https://api-optimistic.etherscan.io/api',
    'MATIC': 'https://api.polygonscan.com/api',
    'AVAX':  'https://api.snowtrace.io/api',
}

TRON_API = 'https://apilist.tronscanapi.com/api'

REQUEST_DELAY = 0.25   # seconds between API calls (rate limit)
TX_LIMIT = 50          # max transactions fetched per address per hop


# ─── Fetch Transactions ───────────────────────────────────────────────────────

def _fetch_evm_txs(address: str, api_base: str, api_key: str) -> list:
    params = {
        'module': 'account',
        'action': 'txlist',
        'address': address,
        'startblock': 0,
        'endblock': 99999999,
        'page': 1,
        'offset': TX_LIMIT,
        'sort': 'desc',
        'apikey': api_key or 'YourApiKeyToken',
    }
    try:
        r = requests.get(api_base, params=params, timeout=12)
        data = r.json()
        if data.get('status') == '1':
            return data.get('result', [])
        return []
    except Exception:
        return []


def _fetch_tron_txs(address: str, api_key: str) -> list:
    try:
        url = f"{TRON_API}/transaction"
        params = {'address': address, 'limit': TX_LIMIT, 'sort': '-timestamp'}
        headers = {'TRON-PRO-API-KEY': api_key} if api_key else {}
        r = requests.get(url, params=params, headers=headers, timeout=12)
        data = r.json()
        return data.get('data', [])
    except Exception:
        return []


def fetch_transactions(address: str, chain: str, api_key: str = '') -> list:
    """Unified transaction fetch. Returns raw tx list."""
    chain = chain.upper()
    if chain == 'TRON':
        return _fetch_tron_txs(address, api_key)
    api_base = EVM_APIS.get(chain, EVM_APIS['ETH'])
    return _fetch_evm_txs(address, api_base, api_key)


def get_neighbors(address: str, chain: str, api_key: str = '') -> list:
    """
    Return list of dicts: {address, tx_hash, value, timestamp, direction}
    for all addresses that interacted with `address`.
    """
    txs = fetch_transactions(address, chain, api_key)
    seen = set()
    neighbors = []
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
                # Convert Wei to ETH string for display
                try:
                    val_eth = int(tx.get('value', 0)) / 1e18
                    val_str = f"{val_eth:.6f}"
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


# ─── BFS Path Finding ─────────────────────────────────────────────────────────

def find_path(
    addr_a: str,
    addr_b: str,
    chain: str = 'ETH',
    api_key: str = '',
    max_depth: int = 3,
    progress_cb=None,
) -> Optional[list]:
    """
    BFS to find the shortest transaction path from addr_a to addr_b.

    Returns a list of hops:
        [
          {'address': addr_a, 'tx_hash': '', 'direction': 'start', ...},
          {'address': intermediate, 'tx_hash': '0x...', ...},
          {'address': addr_b, 'tx_hash': '0x...', ...},
        ]
    or None if not found within max_depth hops.
    """
    addr_a = addr_a.strip().lower()
    addr_b = addr_b.strip().lower()

    if addr_a == addr_b:
        return [{'address': addr_a, 'tx_hash': '', 'value': '0',
                 'timestamp': '', 'direction': 'start'}]

    def prog(msg):
        if progress_cb:
            progress_cb(msg)

    queue = deque()
    start_node = {'address': addr_a, 'tx_hash': '', 'value': '0',
                  'timestamp': '', 'direction': 'start'}
    queue.append((addr_a, [start_node]))
    visited = {addr_a}

    while queue:
        current, path = queue.popleft()
        depth = len(path) - 1

        if depth >= max_depth:
            continue

        prog(f"正在分析 {current[:10]}…（第 {depth+1} 跳，队列 {len(queue)} 个）")
        time.sleep(REQUEST_DELAY)

        neighbors = get_neighbors(current, chain, api_key)
        prog(f"{current[:10]}… 有 {len(neighbors)} 个交互地址")

        for nbr in neighbors:
            nbr_addr = nbr['address']
            new_path = path + [nbr]

            if nbr_addr == addr_b:
                prog(f"找到路径！共 {len(new_path)-1} 跳")
                return new_path

            if nbr_addr not in visited:
                visited.add(nbr_addr)
                queue.append((nbr_addr, new_path))

    prog("未找到关联路径")
    return None


# ─── Batch Address Analysis ───────────────────────────────────────────────────

def analyze_addresses(
    addresses: list,
    chain: str = 'ETH',
    api_key: str = '',
    max_per_addr: int = TX_LIMIT,
    progress_cb=None,
) -> dict:
    """
    Fetch transactions for a list of addresses and build a
    local interaction graph. Returns:
      {
        'nodes': [{'address': ..., 'tx_count': ...}, ...],
        'edges': [{'from': ..., 'to': ..., 'tx_hash': ..., 'value': ...}, ...],
        'paths': {  # direct connections found between input addresses
            'A:B': [hop, hop, ...]
        }
      }
    """
    def prog(msg):
        if progress_cb:
            progress_cb(msg)

    addr_set = {a.strip().lower() for a in addresses if a.strip()}
    all_nodes = {}   # addr -> tx_count
    all_edges = []   # list of edge dicts
    seen_edges = set()

    for addr in addr_set:
        prog(f"获取 {addr[:12]}… 的交易记录")
        time.sleep(REQUEST_DELAY)
        neighbors = get_neighbors(addr, chain, api_key)
        all_nodes[addr] = len(neighbors)

        for nbr in neighbors:
            nbr_addr = nbr['address']
            if nbr_addr not in all_nodes:
                all_nodes[nbr_addr] = 0
            key = tuple(sorted([addr, nbr_addr]))
            if key not in seen_edges:
                seen_edges.add(key)
                all_edges.append({
                    'from': addr,
                    'to': nbr_addr,
                    'tx_hash': nbr['tx_hash'],
                    'value': nbr['value'],
                    'timestamp': nbr['timestamp'],
                })

    # Find direct paths between all pairs of input addresses
    input_addrs = list(addr_set)
    paths = {}
    for i in range(len(input_addrs)):
        for j in range(i + 1, len(input_addrs)):
            a, b = input_addrs[i], input_addrs[j]
            prog(f"寻找 {a[:8]}… → {b[:8]}… 的关联路径")
            # Check if directly connected via fetched edges
            key = f"{a}:{b}"
            connected = [e for e in all_edges
                         if (e['from'] == a and e['to'] == b) or
                            (e['from'] == b and e['to'] == a)]
            if connected:
                paths[key] = [
                    {'address': a, 'tx_hash': '', 'direction': 'start', 'value': '0'},
                    {'address': b, 'tx_hash': connected[0]['tx_hash'],
                     'direction': 'direct', 'value': connected[0]['value']},
                ]

    return {
        'nodes': [{'address': addr, 'tx_count': cnt}
                  for addr, cnt in all_nodes.items()],
        'edges': all_edges,
        'paths': paths,
    }
