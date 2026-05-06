# Examples

## Minesweeper Demo

Play the demo in a browser:

```bash
.venv/bin/python examples/minesweeper_demo.py serve
```

Open <http://127.0.0.1:8765>. The board is a tiny, intentionally buggy
Minesweeper backend. You can manually trigger the same invalid states that
Stipulate discovers automatically.

Run the demo validation:

```bash
.venv/bin/python examples/minesweeper_demo.py validate
```

See the exploration findings:

```bash
.venv/bin/python examples/minesweeper_demo.py explore
```

See the mutation report:

```bash
.venv/bin/python examples/minesweeper_demo.py mutate --budget 60
```

The first wow moment is exploration: it reports orphaned child rows, the
`lost -> won` and `won -> lost` status transitions, and the `revealed ->
flagged` cell transition with reproducing sequences.

The second wow moment is mutation feedback after the four code bugs are fixed:
`mutate` runs against the fixed-code phase by default and shows which missing
invariants/postconditions still let mutants survive. Use `--buggy` if you want
to see why mutation is noisy before fixing the exploration findings.
