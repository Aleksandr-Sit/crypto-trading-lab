---
id: cex-spot-martingale-capped
branch: cex-spot
source_kind: bot_preset
source_ref: https://www.pionex.com/blog/what-is-martingale-bot/
can_backtest: true
timeframe: 1m
instruments: [BTC/USDT, ETH/USDT, SOL/USDT]
venue: bybit
regime: range
status: hypothesis
params:
  initial_order_usd: 10
  price_drop_pct: 2.0           # шаг докупки от последней цены покупки
  drop_scale: 1.0               # множитель шага (1.0 — равные шаги)
  volume_multiplier: 2.0        # мартингейл: объём каждой докупки ×2
  max_safety_orders: 5          # жёсткий лимит — иначе экспонента съедает капитал
  take_profit_pct: 1.5          # от средней цены
  stop_loss_pct: 15             # от средней цены; обязателен
  trailing_take_profit_pct: 0.3
---

# Мартингейл с лимитом докупок (Pionex Martingale/DCA bot)

## Идея
При падении цены докупать с удвоением объёма, чтобы средняя быстро приближалась к цене; закрыть всё на небольшом отскоке от средней. Лимит числа докупок и стоп превращают «бесконечный мартингейл» в ограниченную ставку.

## Правила (кодируемые)

**Вход.** Рыночная покупка `initial_order_usd`. Докупка `k` (1..`max_safety_orders`): цена ≤ `last_buy_price·(1 − price_drop_pct·drop_scale^(k−1)/100)`, объём `initial_order_usd·volume_multiplier^k`.

**Выход.** Продажа всего при `price ≥ avg·(1+take_profit_pct/100)`; с трейлингом: после достижения тейка держать, пока цена не откатится на `trailing_take_profit_pct` от максимума. Стоп: `price ≤ avg·(1 − stop_loss_pct/100)` — рыночно.

**Размер.** Максимум = `initial·Σ_{k=0}^{5} 2^k` = 10·63 = 630 USD на пару. Одна сделка на пару, ≤ 3 пар.

**Таймфрейм и инструменты.** 1m; ликвидные пары спота.

**Издержки.** Taker на входах/стопе (рыночные), maker на тейке; проскальзывание.

## Режим рынка
Боковик/мелкие откаты. Ломается на трендовом падении: стоп на 15 % от средней при 630 USD в позиции — потеря ~95 USD, а выигрыш на сделку ~1,5 % — соотношение требует win-rate > 90 %; это и есть предмет замера.

## Источники
- [Pionex: What Is a Martingale Bot](https://www.pionex.com/blog/what-is-martingale-bot/) — механика: price scale, volume multiplier, max safety orders, TP от средней.
- [Pionex Help: DCA (Martingale) Bot — Trailing Mode](https://support.pionex.com/hc/en-us/articles/49724897544217-DCA-Martingale-Bot-Trailing-Mode) — trailing take profit.

## Чего не хватает для точного кодирования
- Дефолтные числа Pionex не извлечены (страница US-хелпа отдала 404 на 2026-09-06); значения — выбор тикета 13.
