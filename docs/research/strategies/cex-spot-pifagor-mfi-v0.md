---
id: cex-spot-pifagor-mfi-v0
branch: cex-spot
source_kind: author_indicator
source_ref: https://www.pifagor.trade/indicators.html
can_backtest: true
timeframe: 4h
instruments: [BTC/USDT, ETH/USDT, SOL/USDT]
venue: bybit
regime: range
status: hypothesis
params:
  mfi_period: 14
  oversold: 20
  overbought: 80
  confirm_bars: 1
  stop_loss_pct: 4
  position_pct_of_branch: 20
  indicator_version: v0          # v0 — публичное описание; v1 — по скрипту пользователя
---

# Pifagor «MFI Pifagor» — порт v0 (стандартный MFI с порогами)

## Идея
На сайте автора: «Версия MFI с авторскими доработками. Подходит для любого актива и таймфрейма» ([источник](https://www.pifagor.trade/indicators.html)). Доработки не раскрыты — v0 = стандартный Money Flow Index. Цель: иметь бэктестируемую базу до получения скрипта (G08) и сравнивать с публичными сигналами автора (G08.1).

## Правила (кодируемые)

**Вход.** На закрытии 4h: `MFI(mfi_period)[t−1] < oversold` и `MFI[t] ≥ oversold` (выход из зоны перепроданности), подтверждено `confirm_bars` барами. Рыночно на открытии следующего бара.

**Выход.** `MFI[t] > overbought` → продать; стоп `entry·(1 − stop_loss_pct/100)`.

**Размер.** `position_pct_of_branch` % капитала ветки; одна позиция на инструмент.

**Таймфрейм и инструменты.** 4h (автор: «любой таймфрейм» — сетка замера {1h, 4h, 1d}); BTC/ETH/SOL.

**Издержки.** Taker вход/выход, проскальзывание.

## Режим рынка
Боковик/откаты; в тренде вниз — ранние входы.

## Источники
- [pifagor.trade/indicators.html](https://www.pifagor.trade/indicators.html) — описание «MFI Pifagor» (дословно в `docs/research/indicators/pifagor.md`).
- Формула MFI — Gene Quong & Avrum Soudack; типичные пороги 20/80 (стандарт TradingView `ta.mfi`).

## Чего не хватает для точного кодирования
- «Авторские доработки» — неизвестны: `[ИНДИКАТОР — нужен скрипт]`. v1 заменит `compute()` в `strategies/indicators/pifagor.py` с тестом на эталонных значениях (скриншоты автора с датой).
