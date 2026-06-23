# Skill: Text-Based Game Design

## Core Principles

- Text is the medium and the mechanic. Every word of output is both information and atmosphere. Write tightly — verbose room descriptions slow the game down and train players to skip reading.
- Player agency is the primary value. The game exists to respond to player intent. Every design decision should ask: does this give the player more meaningful choices or fewer?
- Consistency builds trust. If pushing a button does something in one room, pushing a button should behave predictably elsewhere. Surprise the player with story; do not surprise them with the parser.
- Fail gracefully. The player will type things you did not anticipate. An unhelpful "I don't understand that" is better than a crash, but a useful "I don't know how to DO that" or "I don't see a WIDGET here" is better still.

## World Design

- Build the world around verbs, not nouns. Ask: what can the player DO here? A room with no meaningful interaction is a corridor — make it short or cut it.
- Keep maps mentally navigable. Players build a mental model of space. More than ~30 locations in a single connected area becomes disorienting without clear landmarks and a map handout.
- Every room needs: a short name (used in the status line), a first-visit description, and a re-visit description (shorter, just the essentials and any changed state).
- Use compass directions as the primary navigation convention unless the setting strongly argues otherwise (e.g. a spaceship might use fore/aft/port/starboard).
- Connect spaces logically. If the player can see one place from another, they should be able to travel toward it.

## Puzzles

- A good puzzle has a clear goal, observable clues, and a logical solution the player can discover through play — not one that requires reading the designer's mind.
- Every puzzle needs at least one in-world hint. The hint should be findable through normal exploration; do not bury it in an optional or easily-missed location.
- Avoid inventory puzzles that require carrying an item from much earlier in the game with no re-acquisition path. Either make the item re-obtainable or place a copy near the puzzle.
- Do not make puzzles gating puzzles unless the alternative path is clearly signalled. Getting permanently stuck with no feedback is the fastest way to lose a player.
- Provide escalating feedback: attempting a wrong solution should give a different response than doing something completely unrelated. "That doesn't seem to help" is better than a generic error.
- Test every puzzle with someone who has not designed it. Puzzle blindness is real — designers cannot unknow what they know.

## Parser and Command Design

- Support synonyms for common verbs: EXAMINE/LOOK AT/X, TAKE/GET/PICK UP, DROP/PUT DOWN, GO NORTH/NORTH/N.
- Implement AGAIN (or G) to repeat the last command — players use it constantly.
- INVENTORY (or I), LOOK (or L), WAIT (or Z), and HELP should always work.
- Distinguish between "I don't understand that verb" and "I don't see that object here" — the latter tells the player their goal is valid but their location is wrong.
- Implement UNDO if the engine supports it. Puzzle-heavy games especially need it — punishing players for experimenting kills exploration.

## Narrative Integration

- Exposition belongs in objects, not room descriptions. A bookshelf description says it holds books; the book you examine tells you the lore.
- Use NPC dialogue and reactive text to reward the player for paying attention. If the player picked up the key, the guard's dialogue should change.
- Time events and ambient messages (sounds, smells, passing NPCs) make the world feel alive without requiring player input.
- End-state messaging matters as much as the ending itself. The final text is what the player carries away. Write it last, after the rest is working, and spend proportionate time on it.

## Technical Design

- Model world state as explicit flags and object locations, not as implicit side-effects. "Has player seen the hidden door?" is a boolean, not a consequence of which commands were run.
- Separate the world model (data) from the parser (input handling) and the renderer (output). This makes testing easier and allows porting between engines.
- Write descriptive IDs for rooms and objects (`NORTH_CORRIDOR`, `BRASS_KEY`) not numeric IDs (`ROOM_14`, `OBJ_7`). You will thank yourself during debugging.
- Track object state explicitly: LOCKED/UNLOCKED, OPEN/CLOSED, LIT/UNLIT, WORN/UNWORN. Do not infer state from object location alone.
- Test every room transition, every puzzle path, and every dead-end (intentional and unintentional) before release.

## Scope and Pacing

- A small, complete, well-tested game is better than a large, broken, unfinished one. Scope down aggressively for first projects.
- Estimate location count early and halve it. Ten well-crafted rooms with three good puzzles makes a satisfying hour of play.
- The opening must hook immediately. The player should have a clear goal, a reason to care, and at least one interesting thing to interact with in the first three moves.
- Vary puzzle difficulty. Follow a hard puzzle with an easy win. Players need momentum.
