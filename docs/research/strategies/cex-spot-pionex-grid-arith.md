---
id: cex-spot-pionex-grid-arith
branch: cex-spot
source_kind: bot_preset
source_ref: https://support.pionex.com/hc/en-us/articles/45085712163225-Grid-Trading-Bot
can_backtest: true
timeframe: 1m
instruments: [BTC/USDT, ETH/USDT]
venue: binance
regime: range
status: hypothesis
params:
  range_mode: auto_30d          # auto_30d — границы = min/max за 30 дней (1d high/low); или явные lower/upper
  lower_price: null
  upper_price: null
  grids: 50
  grid_type: arithmetic         # arithmetic | geometric
  investment_usd: 300
  stop_loss_price_pct_below_lower: 5
  take_profit_price_pct_above_upper: 5
  trailing_up: false
---

# Спот-грид (Pionex, арифметическая сетка)

## Идея
Сетка лимитных ордеров внутри диапазона: каждая покупка на уровне `i` закрывается продажей на уровне `i+1`. Прибыль — с каждого колебания цены между уровнями.

## Правила (кодируемые)

**Вход.** При старте: диапазон `[lower, upper]` (в режиме `auto_30d` — минимум/максимум дневных low/high за 30 дней). Шаг `step = (upper - lower) / grids` (arithmetic) или `ratio = (upper/lower)^(1/grids)` (geometric). Начальная позиция: покупается столько базовой монеты, сколько нужно для продаж на уровнях выше текущей цены; на всех уровнях ниже — лимит-покупки. Каждый исполненный buy на уровне `i` ставит sell на `i+1`; каждый sell на `i` ставит buy на `i-1`.

**Выход.** Стоп-лосс: цена ≤ `lower * (1 - stop_loss_price_pct_below_lower/100)` — закрыть всё рыночно, бот остановлен. Тейк: цена ≥ `upper * (1 + take_profit…/100)` — продать остаток, остановлен. Иначе бесконечно.

**Размер.** `investment_usd` делится поровну на количество уровней (Pionex: «investment split evenly across grids»). Одна сетка на пару.

**Таймфрейм и инструменты.** 1m свечи для проверки касания уровней (по high/low); инструменты — пары с историей ≥ 90 дней и суточным оборотом ≥ 50 млн USD.

**Издержки.** Каждая пара buy/sell — 2 × maker-комиссия; профит на уровень `step/price − 2·maker_bps` должен быть > 0 (Pionex указывает «profit per grid» с учётом комиссии).

## Режим рынка
Боковик; ломается при выходе цены из диапазона (все уровни исполнены — держишь монету на дне или стоишь в USDT на вершине).

## Источники
- [Pionex Help: Grid Trading Bot](https://support.pionex.com/hc/en-us/articles/45085712163225-Grid-Trading-Bot) — параметры lower/upper/grids, arithmetic vs geometric, stop-loss/take-profit.
- [Pionex Blog: Grid Bot parameters](https://www.pionex.com/blog/grid-bot-parameters/) — распределение инвестиции по уровням, «profit per grid».

## Чего не хватает для точного кодирования
- Pionex «AI-strategy» (автоподбор диапазона по 7 дням) не документирован формулой — `auto_30d` принято тикетом 13 как замена.
- Число уровней 50 — выбор тикета 13; замер по сетке {20, 50, 100}.
