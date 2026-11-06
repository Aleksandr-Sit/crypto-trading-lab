#!/bin/bash
# Read-only evidence for the candidate frozen on 2026-09-14. No measurements or orders.
set -euo pipefail
ssh vps-trader 'cd /opt/crypto-trading-lab && docker compose -f deploy/docker-compose.yml exec -T db sh -c "psql -X -q -v ON_ERROR_STOP=1 -U \$POSTGRES_USER -d \$POSTGRES_DB -At"' <<'SQL'
BEGIN READ ONLY;
SELECT jsonb_pretty(jsonb_build_object(
  'captured_at', current_timestamp,
  'schema_version', (SELECT version_num FROM alembic_version LIMIT 1),
  'strategy', (SELECT to_jsonb(s) FROM strategies s
    WHERE id = 'cex-spot-external-rotation-gold-btc'),
  'measurements', (SELECT coalesce(jsonb_agg(jsonb_build_object(
      'id', id, 'mode', mode, 'status', status, 'created_at', created_at,
      'window_from', window_from, 'window_to', window_to, 'code_version', code_version,
      'data_hash', data_hash, 'metrics', metrics_json::jsonb->'metrics',
      'threshold', threshold_json) ORDER BY id), '[]'::jsonb)
    FROM measurements m WHERE strategy_id = 'cex-spot-external-rotation-gold-btc'
      AND created_at >= '2026-09-14T00:00:00+04:00'),
  'transitions', (SELECT coalesce(jsonb_agg(to_jsonb(t) - 'metrics_snapshot'
      ORDER BY id), '[]'::jsonb)
    FROM rung_transitions t WHERE strategy_id = 'cex-spot-external-rotation-gold-btc'),
  'signals', (SELECT jsonb_build_object('count', count(*),
      'first', min(decided_at), 'last', max(decided_at))
    FROM signals WHERE strategy_id = 'cex-spot-external-rotation-gold-btc'),
  'orders_by_mode', (SELECT coalesce(jsonb_agg(to_jsonb(o)), '[]'::jsonb)
    FROM (SELECT mode, count(*) AS count FROM orders
      WHERE signal_id IN (SELECT id FROM signals
        WHERE strategy_id = 'cex-spot-external-rotation-gold-btc') GROUP BY mode) o),
  'trades_by_mode', (SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb)
    FROM (SELECT mode, count(*) AS count, count(closed_at) AS closed,
        min(opened_at) AS first_open, max(closed_at) AS last_close,
        sum(pnl_net) FILTER (WHERE closed_at IS NOT NULL) AS closed_net_pnl
      FROM trades WHERE strategy_id = 'cex-spot-external-rotation-gold-btc'
      GROUP BY mode) t),
  'collector', (SELECT value FROM system_flags
    WHERE key = 'virtual_forward:cex-spot-external-rotation-gold-btc')
));
ROLLBACK;
SQL
