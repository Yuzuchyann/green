# Corporate Greenwashing and Stock Returns

> Does the gap between what A-share firms **say** about the environment and what they
> **actually do** get priced by the stock market?

**Status:** Month 1 of 10 · **Period:** Sep 2026 – Jun 2027
**Full plan:** [`RESEARCH_PLAN.md`](RESEARCH_PLAN.md) · [`plan.html`](plan.html) (Gantt chart)

---

## Why this project

In April 2024, China's three stock exchanges issued the *Sustainability Reporting
Guidelines*, taking effect 1 May 2024. Firms in the SSE 180, STAR 50, SZSE 100 and
ChiNext indices, plus all dual-listed companies, were required to publish their FY2025
sustainability reports by **30 April 2026**.

By the end of April 2026, **2,720 A-share companies** had disclosed — a 49% disclosure
rate, up from 34% two years earlier. Among mandated firms, disclosure hit 100%.

This is a rare natural experiment: for the first time, comparable and standardised ESG
data on thousands of firms landed in the market almost simultaneously, and the data
became complete only in 2026. Firms that had relied on vague green rhetoric now have to
put numbers on the table.

**Research question:** Under mandatory sustainability disclosure, is the degree of
corporate greenwashing — and how is it — priced by the stock market?

---

## Three independent measures of greenwashing

All three are constructed separately, then cross-validated against one another.
Having independent measures converge on the same answer is the strongest robustness
check available here.

| Path | Variable | Construction | Weight |
|---|---|---|---|
| **A** · ESG rating disagreement | `GW_rd` | Std. dev. of cross-agency ESG scores, each converted to a percentile rank within industry-year | 20% |
| **B** · Disclosure–performance gap | `GW_gap` | `z(disclosure score) − z(actual environmental performance)` | 50% |
| **C** · Text / LLM approach | `GW_text` | Percentile of green-rhetoric intensity − percentile of actual environmental spending | 30% |

**Note on standardisation.** Chinese and international rating agencies use completely
different scales (Huazheng uses AAA–C, CSI uses 0–100, SynTao Green Finance uses A+ to D).
Raw scores must be converted to ranks or z-scores **before** any cross-agency comparison —
otherwise the standard deviation is meaningless.

**Note on shared infrastructure.** Path B's "actual performance" indicators depend on the
annual report parsing built in Path C. B and C share one data pipeline, not two.

---

## Empirical design

**Event study (short-run).**
Market model estimated over `[-120, -21]`. CAR windows `(-1,+1)`, `(-2,+2)`, `(-5,+5)`.
Event = each firm's actual FY2025 sustainability report disclosure date (Jan–Apr 2026).
Test = is CAR significantly more negative for high-greenwashing firms?

**Panel regression (long-run).**
`Y_it = β₀ + β₁·GW_it + Σγ·Controls + Firm FE + Year FE + ε`
Standard errors **clustered by firm**. Outcomes: annual return, Tobin's Q, stock price
synchronicity (SYNCH).

**Robustness.** The three greenwashing measures double as robustness checks for each
other; plus lagged regressors, winsorising, PSM, and an industry-year-mean instrument.

### Known limitations (will be stated in the paper)

1. Rating disagreement may reflect methodological differences between agencies rather
   than genuine greenwashing. Cross-validation with the text-based measure mitigates
   but cannot fully rule this out.
2. The sample covers only firms that disclosed, leaving self-selection bias.
3. Other announcements may contaminate the event window, so CAR attribution needs care.

*A null result is still a result. If the market shows no reaction, explaining **why** —
do investors not read these reports, or is the disclosed quality too low to act on — is
more valuable than manufacturing significance.*

---

## Data sources (all free, no paid database required)

| Data | Source |
|---|---|
| Prices, market cap, financials | Tushare · AkShare · BaoStock |
| CSI ESG ratings | China Securities Index Co. (csindex.com.cn) |
| MSCI ESG ratings | MSCI free ESG Ratings search tool |
| Annual & sustainability reports | CNINFO (cninfo.com.cn) |
| Environmental penalties | IPE Blue Map (en.ipe.org.cn) |

---

## Repository structure

```
greenwashing-project/
├── README.md              # this file
├── RESEARCH_PLAN.md       # full research design (Chinese)
├── plan.html              # 10-month Gantt chart & monthly breakdown
├── research-log/          # weekly research notes
├── src/                   # code (to be built)
├── data/                  # not tracked — see .gitignore
│   ├── raw/               # downloaded reports
│   └── processed/         # parsed output
└── .gitignore             # secrets and data are excluded
```

**Secrets:** Tushare tokens and LLM API keys live in `.env`, which is git-ignored.
Never commit them.

---

## Timeline

| Month | Milestone |
|---|---|
| 2026-09 | Repository, literature matrix, AkShare pipeline |
| 2026-10 | Data pipeline — 100-firm pilot |
| 2026-11 | `GW_rd` complete |
| 2026-12 → 02 | `GW_text` complete (incl. human validation) |
| 2026-12 → 03 | `GW_gap` complete |
| 2027-02 → 03 | Econometrics + event study |
| 2027-04 | Main results + robustness |
| 2027-05 | Paper draft |
| 2027-06 | Streamlit app + wrap-up |

Weekly progress is recorded in [`research-log/`](research-log/) — written as it happens,
never backfilled.
