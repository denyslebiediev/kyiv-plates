# kyiv-plates

Available Kyiv license plates numbered 0000–0099, from the ГСЦ МВС checker at
<https://opendata.hsc.gov.ua/check-leisure-license-plates/>.

**Site:** <https://denyslebiediev.github.io/kyiv-plates/>

`.github/workflows/update.yml` runs `plates.py` at :07 and :37 past every hour and commits
`docs/plates.json`, then deploys `docs/` to GitHub Pages. A failed fetch keeps the previous list, and the
page flags data older than 2 hours.
