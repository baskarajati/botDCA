# HYPEUSDT historical validation summary

## Dataset and scope

The private Bybit export contains 765 HYPEUSDT long rows grouped into 305
non-overlapping completed cycles from 2026-06-17 04:09 UTC through 2026-09-17
01:37 UTC. The CSV is not committed. Public Bybit one-minute linear-perpetual
candles provide the replay market path.

Seven leverage/TP regimes were detected. Walk-forward calibration was limited
to regimes 3 and 4 because they contain 148 and 115 cycles respectively. The
remaining 42 cycles were too sparse for a defensible chronological split.
Those excluded regimes still matter for risk: the full export includes two
DCA9 cycles and one DCA10 cycle, while the reconstructed runtime ladder ends at
DCA8. This is an unresolved model mismatch, not evidence that deeper exposure
is safe.

## Validation result

The reconstructed strategy is **not validated**. On the holdout for regime 3,
continuous matching fell from 82.2% with baseline parameters to 57.8% after
training selected a 180-second re-entry delay. This is overfit. Regime 4
continuous matching improved from 48.6% to 62.9%, but remained well below the
90% target. Runtime defaults were therefore not changed and `strategy-v1` is
not ready to lock.

## Entry and re-entry identification

The 304 observed cycle transitions reject a literal fixed 48-second re-entry
rule. On the final 30% chronological holdout (92 transitions), its median
absolute timing error was 13.5 seconds. A scheduler trained only on the first
70%—the next UTC minute boundary plus second 4—reduced median absolute error to
2.0 seconds. It did not explain every transition: 79/92 (85.9%) occurred in the
immediately following minute, while the others skipped one or more minute
boundaries.

The defensible rule is therefore a minute-scheduled entry attempt with an
unobserved admission or pause condition. “Next candle open” is a useful
minute-resolution replay proxy, not proof of the trader's exact order logic.
For the 137 cycles with a directly identifiable DCA0 fill, median absolute
entry-price error was 0.0358% versus the entry-minute open and 0.0787% versus
the close; every price was inside that minute's range.

Long pauses are strongly associated with deep prior cycles but do not reveal a
deterministic cooldown. Five of eight transitions after DCA8 or deeper skipped
more than five scheduled minutes, compared with two of 296 shallower
transitions (one-sided Fisher exact probability 5.52e-8). The deep sample is
too small to infer how long a pause should last or which hidden condition
released it.

## DCA9-DCA10 evidence

The three deep observations belong to different strategy regimes and cannot be
pooled:

| Regime | Level | Samples | Quantity vs prior add | Constrained trigger-drop evidence | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- |
| Older 20x | DCA9 | 1 | 1.4096x | 1.8117%-2.5941% | Continuation of an older geometric ladder |
| Older 20x | DCA10 | 1 | 1.4188x | 2.2369%-2.6216% | Continuation of an older geometric ladder |
| Mature 24x | DCA9 | 2 | 1.0066x-1.0074x | 5.7667%-6.3651% | Candidate capped emergency add |

The trigger ranges are constrained estimates derived from one-minute candle
ranges and the exported final weighted-average entry, not exact fill prices.
Two mature samples are insufficient to add DCA9 to production, and the lone
DCA10 observation is not evidence for the current 24x regime. Runtime defaults
remain unchanged.

## Simulated economics and path risk

Economics cover the 263 cycles in regimes 3 and 4. They describe autonomous
strategy replays, not the trader's actual account P&L.

| Configuration / path | Simulated closes | Gross P&L | Fees paid | Net realized excluding funding | Max regime MTM drawdown |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline / low-first | 215 | 276.80 USDT | 28.10 USDT | 248.71 USDT | 252.96 USDT |
| Baseline / high-first | 205 | 273.44 USDT | 27.76 USDT | 245.69 USDT | 252.96 USDT |
| Calibrated / low-first | 227 | 268.46 USDT | 28.48 USDT | 239.99 USDT | 252.90 USDT |
| Calibrated / high-first | 214 | 266.37 USDT | 28.25 USDT | 238.14 USDT | 252.90 USDT |

The replay reached DCA8, about 1,316 USDT peak mark notional and about 54.24
USDT deployed margin for a 1 USDT base-margin unit at 24x. Worst floating P&L
was approximately -249.78 USDT. The longest simulated cycle was about 35.3 days
and the longest minute-resolution underwater interval was about 29.5 days.
These results show why positive completed cycles are not sufficient safety
evidence.

Fees use a configurable 0.055% taker assumption on every initial entry, DCA,
and close. Funding is not included because timestamped historical rates were
not supplied. Slippage, wallet transfers, other cross-margin positions, and
portfolio offsets are also excluded.

## Approximate post-ladder stress

The baseline ladder grows entry notional from 24 USDT initially to about
1,301.73 USDT after DCA8, a 54.24x multiple. With a normalized 100 USDT opening
price, the final DCA fills around 88.75 and the weighted average is around
91.98.

| Additional drop from final DCA | Floating loss | Approximate equity reserve |
| --- | ---: | ---: |
| 5% | 108.42 USDT | 170.00 USDT |
| 10% | 171.23 USDT | 232.46 USDT |
| 20% | 296.84 USDT | 357.37 USDT |

The reserve proxy equals deployed margin plus floating loss, entry and
estimated close fees, and a configurable 0.5% maintenance buffer. It is not an
exact Bybit UTA liquidation calculation or a recommended account balance.

## Conclusion

The preregistered tail-risk hypothesis is supported: every tested baseline and
calibrated profile required more than 54x the initial entry notional after the
full ladder, and the 20% post-ladder loss was roughly 234x estimated round-trip
fees. Small TP and trigger changes did not remove the dominant geometric
exposure. Entry timing is now narrowed to a next-minute scheduled attempt plus
an unobserved gate, but that gate and the sparse deep-DCA behavior remain
unidentified. Further tuning should wait for more regime-consistent deep-cycle
samples and funding/slippage evidence.
