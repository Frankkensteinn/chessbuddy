# ChessBuddy

A small PyQt6 desktop app that reads the live position of the chess.com or duolingo game open in your browser, renders it on an editable board, and analyzes the position with a local Stockfish engine.

## Prerequisites

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/) — `uv sync` installs PyQt6 and python-chess into `.venv`.
- **Stockfish 18** — no manual install needed: `scripts/install_stockfish.py` auto-detects your OS/CPU and downloads the official prebuilt binaries (Windows, macOS, Linux) into `Stockfish/prebuilt/`. (Cloning the Stockfish repo is **not** required.)
- **WebBridge daemon** *(optional — only for live positions)* — a local WebBridge running at `http://127.0.0.1:10086` to read the position from your open chess.com / duolingo tab. Without it, paste a FEN or edit the board by hand.

## Quickstart

```bash
uv sync                                        # install dependencies
uv run python scripts/install_stockfish.py     # download Stockfish binaries
uv run python scripts/install_stockfish.py --check   # verify engine handshake
uv run chessbuddy                              # launch the app
```

- `STOCKFISH_BIN=/path/to/stockfish.exe` overrides engine discovery if the binary lives elsewhere.
- `uv run python scripts/smoke_test.py` runs an offscreen self-test.

## Position sources

Pick the source in the toolbar (before **Fetch position**); the choice is remembered:

| Source | What one fetch returns | Toolbar extras |
|---|---|---|
| `chess.com` | the live FEN, read straight off `<wc-chess-board>`'s game model | — |
| `duolingo` | the board **canvas as a PNG** *and* the **full move history**, from a single `evaluate` | **Preview image** shows the captured PNG · **History** scrubs the whole game on the board |

With `duolingo` the **move history is the source of truth**: the FEN is replayed from the standard start position with python-chess, and the derived position is checked against the canvas. The image is only ever a picture for you to look at.

One exception is what makes a lesson match usable. A lesson commits *your* move to that history immediately, but the opponent's scripted reply only when you submit your **next** move — so for the whole of your turn the history is one ply short while the board on screen already shows the reply. A history-only fetch was therefore a move behind, and named the wrong side to move (`black to move` while it was really your turn). Nothing else in the page carries that ply — checked on 2026-09-19: the board component's props, `challengeState`, the challenge's `opponentMove`, `challenge.match`, the redux store, every ref on the canvas's ancestor chain, and the DOM (no move list is rendered) — so it is read back off the canvas itself by `duolingo_canvas.py`. Only *occupancy* is read (empty / white piece / dark piece, all 64 squares); among the **legal** moves of the position the history already describes, the single one whose result reproduces the board exactly is appended. A misread can thus never invent a move, it can only fail to match. The status bar reports it (`reply a6 read off the board`) and **History** then scrubs that ply too.

Both Duolingo match kinds work — lesson/course challenges (`challenge challenge-chessMatch`) and PvP matches (`challenge challenge-chessPvpMatch`). They differ in where the history lives: a lesson keeps it under `challengeState.guess`, while a PvP match leaves that empty and publishes the real `moveHistory` **plus** its own `boardFen` in the board component's props. PvP is therefore read from those props, and the two readings are cross-checked — if they ever disagree the fetch fails instead of guessing. That cross-check owns correctness on the PvP path, so a canvas that disagrees with it is reported rather than allowed to add a ply. The cross-check normalises FEN's en-passant field before comparing: the page records the square after every double pawn push, while a replayed game only records it while a capture is actually available, so `... b - f3` and `... b - -` are the same position.

**Preview image** and **History** are duolingo-only: while `chess.com` is selected they are greyed out and struck through. (Greyed-out *without* a strike means "duolingo is selected, but nothing has been fetched yet".) A fetched snapshot survives a source switch, so flipping back to `duolingo` re-enables both.

Two things worth knowing:

- Duolingo paints **only the live position** onto its canvas, so the preview image is always the live position — never the ply you are scrubbing. The image is replaced outright by the next fetch, and dropped after 60s without one.
- The reader depends on brittle DOM anchors (the `challenge challenge-chess*` container, the React `challengeState` prop, and the board component's own props on PvP) — and, for the uncommitted reply, on the canvas's own pixels. If Duolingo ships a front-end change, the fetch fails with an explicit message rather than guessing a position. `scripts/smoke_test.py` re-checks that every anchor string is still present in the reader, and pins the canvas geometry and palette by rendering a duolingo-shaped board and reading it back.
- When the board cannot be lined up with the history at all — a piece mid-animation, or one held in your hand — the fetch says so in the status bar (`⚠ the board on screen could not be lined up…`) and keeps the history's position rather than inventing a ply. Fetch again once the board settles. The read is already retried three times, 250 ms apart, for exactly this.
- Duolingo's own move list has a back-stepping UI. If you have scrubbed back in it, the canvas shows that ply and the fetch refuses rather than filing a stale position under a "live" label — return to the live position and fetch again.

## What-if view

Once **Analyze** has finished, **What-if** turns the analysed position into a move tree on an infinite canvas. It is a separate page with its own board, and the two views share one cursor: leaving the graph puts the board on the node you were last looking at.

- **Entering is free.** The three engine lines become three lanes — each line laid out as a chain of nodes, x = ply, y = lane — without a single new search.
- **Grey vs lit is the whole point.** A grey node is a prediction from an engine line: it has a position (click it and the board jumps there) but no evaluation yet. Clicking it runs one 600 ms `MultiPV=3` search which both lights it up — eval, depth, and its Δ against the parent's best move — and remembers its three continuations, so **Enter** (or the `+` under a node) can lay those candidates out with no further search at all.
- **Drag a piece** on the dock board to branch. Any legal move of the side to move becomes a child, is searched straight away, and gets its own Δ — the same centipawns Blunder check reports, computed against whatever the parent's search said. Branching is unbounded: a new branch can be branched again.
- **Keys:** `←`/`→` walk the line, `↑`/`↓` change lanes, `Home` to the root, `Esc` back to the board, `Enter` to expand. Pan by dragging the background, wheel to scroll, Cmd/Ctrl+wheel (or `+`/`-`) to zoom, `0` to fit. Zoomed out, nodes drop to shapes and then to plain blocks so the outline of the tree — forks, chains, empty space — stays readable.
- **Leaving keeps everything.** Moving the board to the cursor is a display move, exactly like replaying a line: the analysis and the tree both survive, so What-if resumes where you were. Editing or re-fetching the position *does* drop the tree, because the tree belongs to the position it grew from.
- **Shallow is labelled as shallow.** Every number carries its depth, and a Δ under 10 cp is reported as the noise band rather than as a different choice: a 600 ms search is a starting point, not a verdict.

Layout rules, the Δ arithmetic, the engine budget and the deliberate non-goals are specified in `docs/whatif-graph-plan.md`; `scripts/smoke_test.py` covers the model's invariants (node identity, lane insertion, Δ) and the view end to end (entry, expand, branch, re-entry, both themes).

---

Board piece SVGs: [cburnett chess set](https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces) (CC BY-SA 3.0, via Wikimedia Commons).
