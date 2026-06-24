# Skill: Text-Based Game Design

## Core Principles

- Text is the medium and the mechanic. Every word of output is both information and atmosphere. Write tightly — verbose room descriptions slow the game down and train players to skip reading.
- Player agency is the primary value. The game exists to respond to player intent. Every design decision should ask: does this give the player more meaningful choices or fewer?
- Consistency builds trust. If pushing a button does something in one room, pushing a button should behave predictably elsewhere. Surprise the player with story; do not surprise them with the parser.
- Fail gracefully. The player will type things you did not anticipate. A useful "I don't know how to DO that" or "I don't see a WIDGET here" is far better than a generic error.

## World Design

- Build the world around verbs, not nouns. Ask: what can the player DO here? A room with no meaningful interaction is a corridor — make it short or cut it.
- Keep maps mentally navigable. Players build a mental model of space. More than ~30 locations in a single connected area becomes disorienting without clear landmarks.
- Every room needs: a short name (status line), a first-visit description, and a re-visit description (shorter, just the essentials and any changed state).
- Use compass directions as the primary navigation convention unless the setting strongly argues otherwise.
- Connect spaces logically. If the player can see one place from another, they should be able to travel toward it.

## NPC Design

- NPCs should have a want of their own. Even a shopkeeper wants to close early; even a guard wants an easy shift. The conflict between their want and the player's goal is the drama.
- Give NPCs consistent, predictable behaviour before introducing surprising behaviour. The player must understand the rules before they can cleverly subvert them.
- Use conversation trees sparingly — long branching dialogue is expensive to write and players skip it. Short, reactive responses tied to game state are more effective.
- NPCs that react to what the player has done elsewhere make the world feel alive. The blacksmith knows you killed the bandit chief; the innkeeper heard about it too.

## Puzzles

- A good puzzle has a clear goal, observable clues, and a logical solution the player can discover through play — not one that requires reading the designer's mind.
- Every puzzle needs at least one in-world hint. The hint should be findable through normal exploration.
- Avoid inventory puzzles that require carrying an item from much earlier with no re-acquisition path. Either make the item re-obtainable or place a copy near the puzzle.
- Provide escalating feedback: attempting a wrong solution should give a different response than doing something completely unrelated.
- Test every puzzle with someone who has not designed it. Puzzle blindness is real.

## Parser and Command Design

- Support synonyms for common verbs: EXAMINE/LOOK AT/X, TAKE/GET/PICK UP, DROP/PUT DOWN, GO NORTH/NORTH/N.
- Implement AGAIN (or G) to repeat the last command — players use it constantly.
- INVENTORY (or I), LOOK (or L), WAIT (or Z), and HELP should always work.
- Distinguish between "I don't understand that verb" and "I don't see that object here" — the latter tells the player their goal is valid but their location is wrong.
- Implement UNDO if the engine supports it. Puzzle-heavy games especially need it.

## Disambiguation

When the player types something ambiguous, ask a clarifying question rather than guessing:
```
> take key
Which do you mean, the brass key or the rusty key?
```
Never silently pick one — if you pick wrong, the player has no idea what happened.

## Narrative Integration

- Exposition belongs in objects, not room descriptions. A bookshelf says it holds books; the book you examine tells you the lore.
- Use NPC dialogue and reactive text to reward attention. If the player picked up the key, the guard's dialogue should acknowledge it.
- Time events and ambient messages (sounds, smells, passing NPCs) make the world feel alive.
- End-state messaging matters as much as the ending itself. The final text is what the player carries away — write it after the rest is working and spend proportionate time on it.

## Technical Design

- Model world state as explicit flags and object locations, not as implicit side-effects. "Has player seen the hidden door?" is a boolean, not a consequence of which commands were run.
- Separate the world model (data) from the parser (input handling) and the renderer (output). This makes testing easier and allows porting between engines.
- Write descriptive IDs for rooms and objects (`NORTH_CORRIDOR`, `BRASS_KEY`) not numeric IDs (`ROOM_14`, `OBJ_7`).
- Track object state explicitly: LOCKED/UNLOCKED, OPEN/CLOSED, LIT/UNLIT, WORN/UNWORN. Do not infer state from object location alone.

```
Room: LIGHTHOUSE_TOP
  description_first: "The light mechanism fills most of the room..."
  description_revisit: "The mechanism ticks steadily."
  exits: { DOWN: LIGHTHOUSE_STAIRS }
  objects: [LENS, CONTROL_LEVER]
  flags: { light_on: false }

Object: CONTROL_LEVER
  synonyms: ["lever", "handle", "switch"]
  verbs:
    PULL/PUSH/MOVE:
      if light_on: "The lever clicks and the light extinguishes."
      else:        "The lever clicks and the great light blazes to life."
      effect: toggle(LIGHTHOUSE_TOP.flags.light_on)
```

## Scope and Pacing

- A small, complete, well-tested game is better than a large, broken, unfinished one. Scope down aggressively for first projects.
- Ten well-crafted rooms with three good puzzles makes a satisfying hour of play.
- The opening must hook immediately. The player should have a clear goal, a reason to care, and at least one interesting thing to interact with in the first three moves.
- Vary puzzle difficulty. Follow a hard puzzle with an easy win. Players need momentum.
- Signal progress. When the player solves something significant, something in the world should visibly change in response.

## Testing

- Play through your own game fresh after a week away — you will find paths you forgot to test.
- Document every solvable path as a walkthrough. If you cannot write the walkthrough, the game has a dead end.
- Test every room transition and every puzzle path.
- Test what happens when the player tries the obvious wrong solutions — the responses should be informative, not dismissive.
