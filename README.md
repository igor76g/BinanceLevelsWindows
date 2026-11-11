# Binance Levels Monitor 1.0.0 (Windows)

Scans all Binance USDT-M perpetual futures (public endpoints only, no API keys) and warns when the price
comes close to a **cascade of levels**.

## Definitions
- **Extreme** - a swing high / swing low: a candle whose high (low) is the highest (lowest) among N candles
  on each side (default 3). Candle size and history are configurable (default 5m candles, 288 = 24 h).
- **Cascade** - at least 2 extremes (configurable) of the same kind whose prices lie within 0.2 % of each other
  (configurable). Highs form a *resistance* cascade, lows a *support* cascade. A cascade that the price has already
  closed beyond after its last extreme is considered broken and is dropped.
- **Average 1-minute candle** - mean (high - low) of the 1-minute candles of the last hour.
- **Approach** - distance from the current price to the cascade <= 1 average 1-minute candle (configurable).

## Window
1. **Approaching a cascade** - coins that are close enough. New entries trigger sound + popup + Windows toast.
   Cooldown 30 min per coin and side (saved on disk).
2. **Closest to a cascade** - the 5 coins nearest to their cascade (distance in average 1-minute candles),
   whether or not they are close enough. No notifications.

Double-click a ticker -> opens the coin in Vataga.terminal. Click "Copy" in a row -> copies the ticker.

## Settings (all editable)
24h volume (default 30M USDT), NATR of the last 15 min (default 0 = off), trades in the last 15 min (default 0 = off),
candle size / history / swing strength / minimum extremes / tolerance, approach distance, cooldown, check interval.

## Vataga.terminal
Settings > General > "Application integration" on; link the window to the "Screener" group.
Partner id `BinanceLevelsMonitor`, port 5005 (Settings dialog -> Test Vataga).

## Build
Upload the folder (including hidden `.github`) to GitHub -> Actions -> "Build Windows installer" ->
download the artifact `BinanceLevelsMonitor-Setup`. Or locally: `build.bat` (needs Python and Inno Setup 6).

## Traffic
REST only. Every check: 1 request for all tickers + a few new 1-minute candles per coin. Cascades are recalculated
every 5 minutes (1 request per coin with more than 30M volume, weight 2). Far below Binance limits.

## Tests
`python -m unittest discover -s tests`
