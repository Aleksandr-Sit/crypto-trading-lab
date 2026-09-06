---
id: dex-perp-hl-oi-breakout
branch: dex-perp
source_kind: spec
source_ref: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
can_backtest: true
timeframe: 1h
instruments: "перпы Hyperliquid с OI ≥ 20 млн USD"
venue: hyperliquid
regime: high_vol
status: hypothesis
params:
  breakout_lookback: 24
  oi_change_min_pct: 5           # рост OI за 24 ч ≥ 5 %
  volume_mult_min: 1.5           # объём бара ≥ 1.5 × среднего за 24 бара
  stop_atr_mult: 1.5
  take_profit_atr_mult: 3.0
  atr_period: 14
  leverage: 3
  risk_per_trade_pct: 1.0
  max_positions: 3
---

# Пробой на Hyperliquid с подтверждением ростом OI и объёма

## Идея
Пробой суточного диапазона, подтверждённый притоком открытого интереса (новые позиции, не закрытие старых) и объёмом, реже оказывается ложным. Данные OI/объёма по всем перпам доступны в одном ответе `metaAndAssetCtxs`.

## Правила (кодируемые)

**Вход.** На закрытии 1h: long, если `close > max(high[−24..−1])`, `OI[t]/OI[t−24] − 1 ≥ oi_change_min_pct/100`, `volume[t] ≥ volume_mult_min·mean(volume[−24..−1])`. Short — зеркально по `min(low)`.

**Выход.** Стоп `entry ∓ stop_atr_mult·ATR(atr_period)`; тейк `entry ± take_profit_atr_mult·ATR`; по времени — 48 баров.

**Размер.** Риск `risk_per_trade_pct` % капитала ветки на стоп; плечо ≤ 3; ≤ 3 позиций.

**Таймфрейм и инструменты.** 1h свечи (`candleSnapshot`, 5000 баров — ~208 дней истории на 1h; OI-история хранится своим сборщиком с момента запуска — до этого бэктест только по цене).

**Издержки.** Taker 4,5 bps на вход/стоп, maker на тейке; фандинг каждый час.

## Режим рынка
Высокая волатильность/начало тренда; в боковике — ложные пробои.

## Источники
- [Hyperliquid API: Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) — `metaAndAssetCtxs` (`openInterest`, `dayNtlVlm`, `funding`), `candleSnapshot`.
- Идея пробоя — Turtle System 1 (`cex-perp-turtle-donchian`); OI-фильтр — правило тикета 13 (нет внешнего первоисточника → `source_kind: spec`).

## Чего не хватает для точного кодирования
- Истории OI в API нет — первые бэктесты без OI-фильтра (`oi_change_min_pct: 0`), полная версия после накопления данных.
