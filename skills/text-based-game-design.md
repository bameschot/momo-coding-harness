# Skill: Text-Based Game Design

## Core rules

- Write output tightly. Verbose room descriptions slow the game and train players to skip reading.
- Player agency is the point. Ask of every decision: does this give more meaningful choices, or fewer?
- Be consistent. Surprise the player with story, never with the parser.
- Fail gracefully: "I don't see a WIDGET here" beats a generic error, and tells the player what's wrong.
- Build the world around verbs (what can the player DO here?), not nouns.
- Model world state as explicit flags and object locations, never as implicit side effects.

## World

- Every room needs a short name (status line), a first-visit description, and a shorter re-visit description.
- Use compass directions for navigation unless the setting argues otherwise.
- Keep areas mentally navigable — more than ~30 connected rooms disorients without clear landmarks.
- A room with no meaningful interaction is a corridor — make it short or cut it.

## NPCs

- Give each NPC a want of their own; the conflict with the player's goal is the drama.
- Establish predictable behaviour before any surprising behaviour.
- Keep dialogue short and reactive to game state — players skip long branching trees.

## Puzzles

- A good puzzle has a clear goal, observable clues, and a logical solution discoverable through play.
- Every puzzle needs at least one findable in-world hint.
- Avoid puzzles needing an item from much earlier with no re-acquisition path.
- Test every puzzle with someone who did not design it — puzzle blindness is real.

## Parser

- Support verb synonyms: EXAMINE/LOOK AT/X, TAKE/GET, GO NORTH/N. Keep INVENTORY, LOOK, WAIT, HELP, AGAIN always working.
- Distinguish "I don't understand that verb" from "I don't see that object here."
- When a command is ambiguous, ask — never silently pick:

```
> take key
Which do you mean, the brass key or the rusty key?
```

## State model

- Use descriptive IDs (`BRASS_KEY`, `NORTH_CORRIDOR`), not numeric ones.
- Track object state explicitly: LOCKED/UNLOCKED, OPEN/CLOSED, LIT/UNLIT. Don't infer it from location.
- Separate world model (data) from parser (input) from renderer (output).

## Scope

- A small, complete, tested game beats a large broken one. Scope down aggressively.
- Hook the player in the first three moves: a clear goal, a reason to care, one thing to interact with.
- Signal progress — when the player solves something, something in the world visibly changes.
- Document every solvable path as a walkthrough. If you can't write it, there's a dead end.
