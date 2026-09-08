---
id: cex-spot-xsmom-alts-weekly
branch: cex-spot
source_kind: paper
source_ref: https://doi.org/10.1111/jofi.13119
can_backtest: true
timeframe: 1d
instruments: "топ-50 USDT-пар Binance по обороту за 30 дней, исключая стейблы и leveraged-токены"
venue: binance
regime: trend
status: hypothesis
params:
  universe_size: 50
  lookback_days: 21
  skip_days: 1
  top_n: 5
  rebalance_days: 7
  min_daily_volume_usd: 20000000
  btc_filter_sma_days: 100       # покупать только если BTC > SMA(100)
  # стоп на ОДНУ сделку; имя не stop_loss_pct — под тем именем заводится стоп всей стратегии
  trade_stop_pct: 15
  btc_instrument: BTC/USDT
---

# Кросс-секционный моментум альтов (топ-N за 3 недели)

## Идея
Из вселенной ликвидных альтов покупать `top_n` с лучшей доходностью за 3 недели, держать неделю. Liu–Tsyvinski–Wu документируют моментум-премию на 1–4-недельных горизонтах в крипте.

## Правила (кодируемые)

**Вход.** Раз в `rebalance_days` дней: вселенная — `universe_size` пар с наибольшим 30-дневным оборотом и суточным оборотом ≥ `min_daily_volume_usd`. Ранг по `close[t−skip_days]/close[t−skip_days−lookback_days] − 1`. Покупать `top_n` лучших равными долями, если `BTC close > SMA(btc_filter_sma_days)`; иначе всё в USDT.

**Выход.** На следующем ребалансе — продать всё, что выпало из `top_n`. Дополнительно стоп −15 % от входа (рыночно).

**Размер.** Капитал ветки / `top_n` на позицию.

**Таймфрейм и инструменты.** 1d; вселенная пересчитывается на каждом ребалансе (без look-ahead: оборот считается по данным до `t`).

**Издержки.** Taker; оборот портфеля до 100 %/нед — комиссии существенны, считать обязательно.

## Режим рынка
Альт-сезон; в медвежьем — фильтр по BTC держит в кэше.

## Источники
- Liu, Tsyvinski, Wu (2022), «Common Risk Factors in Cryptocurrency», J. Finance — [DOI](https://doi.org/10.1111/jofi.13119): моментум по 1–4-недельным доходностям, размер и оборот как факторы.

## Чего не хватает для точного кодирования
- Delisted-пары: бэктест должен использовать список пар на дату (survivorship) — данные с data.binance.vision содержат делистнутые пары, использовать их.
