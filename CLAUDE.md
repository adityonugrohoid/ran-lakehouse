# CLAUDE.md - ran-lakehouse

A mini telecom lakehouse and data generator for a synthetic 4G (LTE) and
2G (GSM) multi-vendor operator. `README.md` is the human overview;
`docs/spec.md` is the rulebook, and code and reports cite its rules by id
(for example "rule D5").

## Rules for this repo

1. The network, its traffic and its faults are synthetic, generated from
   seeds. Every result says so. The network is modelled on public facts
   about Indonesian networks (bands, technology mix, the WIB time
   zone). No real operator, network, site, city, client or person is
   named, and no real coordinates are used: coordinates are a local km
   grid.
2. `docs/spec.md` is authoritative. A change to a rule is its own pull
   request that edits the spec first.
3. Standards are cited by document, release or edition, and clause
   (3GPP TS 32.435, TS 32.432, TS 32.425, TS 32.450, TS 52.402, TS 32.410,
   ITU-R P.526 and so on). Any other constant is labelled ASSUMPTION.
   Vendor file formats and counter names are "modelled on" public
   descriptions, never presented as vendor copies.
4. Every planted data-quality case has a test that fails loudly when the
   property breaks. Planted answers live only in the evaluation-only
   table; the serving API never returns them.
5. Every published number comes from committed code and a report written
   from one data record (.md plus .json). No number from memory.
6. Generated data, lake storage and run outputs stay in `data/`, `lake/`,
   `landing/` and `runs/`, never committed. `results/` holds reports and
   small figures. External datasets are fetched by script, never
   committed.
7. Code fails loudly: no silent fallbacks, no bare except.
8. History and files carry no plan labels: no version, phase, stage or
   step labels and no pull request numbers in code, comments, docs,
   commit messages or pull request text. Each change names itself.
9. Personal repo: no employer or client name, branding or detail.
