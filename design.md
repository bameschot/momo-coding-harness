# Stellar Trader: A Terminal Space Exploration Game

## Overview
A text-based roguelike space exploration game where players chart unknown galaxies, discover diverse civilizations across random seed-generated star systems, and engage in dynamic trading economies with fluctuating resource prices. The terminal-style interface delivers status screens, market data, and narrative descriptions that build a living universe as you explore it—completely fresh on every new run with no permanent progression between sessions.

## Goals
- Deliver an engaging loop of exploration, discovery, and commerce within a self-contained 1–2 hour play session (or longer if desired).
- Create a sense of wonder through procedurally generated alien worlds with unique resource compositions, civilization hints, and atmospheric descriptions.
- Simulate realistic market dynamics where supply/demand across the galaxy cause prices to rise or fall based on local conditions.
- Maintain tension between curiosity-driven exploration and practical trading needs—should you seek rare resources in distant sectors or exploit current high-value commodities nearby?

## Key Features

### Exploration System
- **Sector-based universe**: The game world is divided into navigable star system "sectors" linked by hyperlanes (wormholes). Each sector generates randomly per new game seed.
- **Planet discovery**: Visiting a sector reveals 1–3 planets with detailed profiles including:
  - World name, atmosphere type, and environmental quirks (e.g., magnetic storms, bioluminescent jungles)
  - Primary resources available on the surface (minerals, organic compounds, rare isotopes, etc.)
  - Estimated resource quantities that fluctuate slightly per visit to simulate extraction limits
- **Navigation mechanics**: Players choose which sector hyperlane to jump through; uncharted routes are discovered dynamically and can become known trade corridors.

### Trading Economy
- **Resource catalog**: Each world offers from a pool of ~20 possible resources (e.g., "neutronium ore," "quantum crystals," "bio-synth protein") with associated base values and scarcity modifiers.
- **Dynamic pricing engine**: 
  - Base price per resource fluctuates based on global supply/demand, which changes as players harvest from multiple worlds
  - Hot/cold market events triggered when a planet's resources are overexploited (prices crash) or neglected (gluts build up elsewhere)
  - Random news events influence markets: "Ice giant sector reports nova activity!" causing ice-adjacent materials to spike overnight.
- **Inventory management**: 
  - Ship hold space limited by cargo capacity which can be filled with various resource combinations
  - Real-time pricing display before selling/buying, showing best current market rates across all known sectors

### Interface & Display
- **Terminal aesthetic**: Green-on-black monospace output simulating a shipboard console; ASCII art for ships/stars (optional) or simple status indicators
- **Command prompt system**: Players type commands like `EXPLORER`, `TRADE <sector>`, `SELL all`, `MARKET STATUS` from an in-game CLI
- **Progressive information disclosure**: Early game shows only basic planet types; as you explore, more detailed civilization hints appear (e.g., "this world hosts a carbon-based society" vs. early generic descriptions)

### Random Seed Generation
- Universe parameters regenerated fresh per session including:
  - Number of sectors and hyperlane topology
  - Planet compositions and resource abundance levels
  - Initial market price ranges based on weighted rarity tables (common minerals abundant, exotic materials scarce)

## Out of Scope
- Combat encounters or weapon systems; the game focuses exclusively on peaceful trade relations
- Persistent world state—each playthrough begins with a brand-new galaxy seed independent of previous games
- Multiplayer functionality; single-player focused experience
- Detailed character customization beyond ship configuration choices if implemented later (initial design: no upgrades, pure procedural sandbox)

## Open Questions
1. Should the random sector count vary per game or stay fixed at an optimal challenge level? 
2. How granular should resource extraction feel—do you want players to see quantities like "50 tonnes available" vs. just categorical labels ("plentiful", "scarce")?
3. Do you envision any narrative tie-ins where particular planet discoveries unlock lore fragments that get displayed between commands?
