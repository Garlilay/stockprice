"""
Arkham Intelligence API client + address label classification.

Skip logic (pruning ~80% of noise):
  SKIP  → CEX hot wallets, DEX pools, bridges, protocol contracts
  KEEP  → CEX deposit addresses, individual wallets, unknown/unlabeled
"""

import time
import requests
from typing import Optional

ARKHAM_BASE = 'https://api.arkhamintelligence.com'

# ─── Classification Rules ─────────────────────────────────────────────────────

# Entity types that are always noise (no individual behind them)
SKIP_ENTITY_TYPES = {'defi', 'bridge', 'nft', 'miner', 'dao', 'token'}

# Label keywords that indicate a dead-end node
SKIP_LABEL_KEYWORDS = [
    'hot wallet', 'cold wallet', 'treasury',
    'pool', 'liquidity', 'amm', 'vault', 'router',
    'deployer', 'fee', 'reward', 'airdrop',
    'burn', 'null', 'wrap', 'unwrap',
    'bridge', 'relay', 'multisig',
]

# Label keywords that indicate we SHOULD continue (individual traceability)
KEEP_LABEL_KEYWORDS = [
    'deposit',     # CEX deposit → traceable to specific user
    'withdrawal',
    'user',
    'individual',
    'personal',
    'wallet',      # generic wallet label, not hot/cold
]

# Well-known protocol/exchange entity names whose hot wallets we skip
SKIP_ENTITY_NAMES = {
    'uniswap', 'curve', 'aave', 'compound', 'balancer',
    'sushiswap', '1inch', 'dydx', 'gmx', 'pancakeswap',
    'lido', 'maker', 'yearn', 'convex', 'synthetix',
    'opensea', 'blur',
    'tornado cash', 'railgun', 'aztec',   # mixers — flag but keep
}


def classify_address(arkham_info: Optional[dict]) -> dict:
    """
    Return classification dict:
      {
        'skip': bool,
        'reason': str,         # why skipped or kept
        'label': str,          # human-readable label
        'entity': str,         # entity name
        'entity_type': str,    # cex / defi / individual / ...
        'is_mixer': bool,
        'is_cex_deposit': bool,
      }
    """
    default = {
        'skip': False, 'reason': 'unlabeled',
        'label': '', 'entity': '', 'entity_type': '',
        'is_mixer': False, 'is_cex_deposit': False,
    }

    if not arkham_info:
        return default

    entity = arkham_info.get('arkhamEntity') or {}
    lbl    = arkham_info.get('arkhamLabel') or {}

    entity_name = (entity.get('name') or '').strip()
    entity_type = (entity.get('type') or '').strip().lower()
    label_name  = (lbl.get('name') or '').strip()

    result = {**default,
              'entity': entity_name,
              'entity_type': entity_type,
              'label': label_name or entity_name}

    label_lower  = label_name.lower()
    entity_lower = entity_name.lower()

    # ── Mixer detection (keep but flag) ──
    if any(m in entity_lower for m in ('tornado', 'railgun', 'aztec', 'mixer')):
        result.update({'skip': False, 'reason': 'mixer', 'is_mixer': True})
        return result

    # ── CEX deposit address: always trace deeper ──
    if 'deposit' in label_lower:
        result.update({'skip': False, 'reason': f'CEX deposit ({entity_name})',
                       'is_cex_deposit': True})
        return result

    # ── KEEP keywords override skip ──
    if any(kw in label_lower for kw in KEEP_LABEL_KEYWORDS):
        result.update({'skip': False, 'reason': f'individual ({label_name})'})
        return result

    # ── Skip by entity type ──
    if entity_type in SKIP_ENTITY_TYPES:
        result.update({'skip': True, 'reason': f'protocol ({entity_type}: {entity_name})'})
        return result

    # ── Skip known protocol/exchange names (hot/cold wallet) ──
    if any(name in entity_lower for name in SKIP_ENTITY_NAMES):
        result.update({'skip': True, 'reason': f'known protocol ({entity_name})'})
        return result

    # ── Skip by label keywords ──
    for kw in SKIP_LABEL_KEYWORDS:
        if kw in label_lower:
            result.update({'skip': True, 'reason': f'label match "{kw}" ({label_name})'})
            return result

    # ── CEX entity without "deposit" label = hot wallet territory → skip ──
    if entity_type == 'cex' and entity_name:
        result.update({'skip': True, 'reason': f'CEX hot wallet ({entity_name})'})
        return result

    # Default: unknown → keep
    result.update({'skip': False, 'reason': 'unlabeled individual'})
    return result


# ─── Arkham API Client ────────────────────────────────────────────────────────

def fetch_arkham_label(address: str, api_key: str, chain: str = 'ethereum') -> Optional[dict]:
    """
    Query Arkham Intelligence for address entity/label info.
    Returns raw API response dict or None on failure.
    """
    if not api_key:
        return None
    url = f"{ARKHAM_BASE}/intelligence/address/{address}"
    headers = {'API-Key': api_key}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(2)
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                return r.json()
        return None
    except Exception:
        return None


def get_label_with_cache(address: str, api_key: str, db_conn) -> dict:
    """
    Get Arkham label for address, using SQLite cache.
    db_conn: sqlite3.Connection with arkham_cache table.
    Returns classification dict.
    """
    address = address.lower()

    # Check cache first
    row = db_conn.execute(
        "SELECT entity_name, entity_type, label, skip, skip_reason, is_mixer, is_cex_deposit "
        "FROM arkham_cache WHERE address = ?", (address,)
    ).fetchone()

    if row:
        return {
            'entity': row['entity_name'] or '',
            'entity_type': row['entity_type'] or '',
            'label': row['label'] or '',
            'skip': bool(row['skip']),
            'reason': row['skip_reason'] or '',
            'is_mixer': bool(row['is_mixer']),
            'is_cex_deposit': bool(row['is_cex_deposit']),
            'from_cache': True,
        }

    # Fetch from Arkham
    raw = fetch_arkham_label(address, api_key)
    result = classify_address(raw)

    # Store in cache
    try:
        db_conn.execute(
            """INSERT OR REPLACE INTO arkham_cache
               (address, entity_name, entity_type, label, skip, skip_reason, is_mixer, is_cex_deposit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (address,
             result['entity'], result['entity_type'], result['label'],
             int(result['skip']), result['reason'],
             int(result['is_mixer']), int(result['is_cex_deposit']))
        )
        db_conn.commit()
    except Exception:
        pass

    result['from_cache'] = False
    return result
