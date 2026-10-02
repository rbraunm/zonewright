"""The words the asset catalog describes client assets with: a category per asset kind and tag groups shared by all kinds, each term
with its meaning, so a description written once reads the same everywhere and a need can be searched for in the same words. Terms added
later live in the catalog's own vocabulary file and merge with these."""

kinds = {
  "texture": "An image a material draws: a diffuse color map, a normal map, a sky or effect image.",
  "model": "A placeable mesh: an EQG model or a classic object actor.",
  "light": "A light style: the lights a zone places under one name stem (or one color and radius), with their color and reach.",
  "emitter": "A particle effect definition, by its client index, with the names zones place it under.",
  "ecosystem": "An EQ terrain zone's ground recipe: texture layers chosen by slope and height.",
}

categories = {
  "texture": {
    "ground": "Walkable natural ground in general when nothing narrower fits.",
    "grass": "Grass and meadow ground.",
    "dirt": "Bare earth, soil, and trodden ground.",
    "sand": "Sand and desert ground.",
    "mud": "Wet earth and swamp ground.",
    "gravel": "Loose stones, scree, and pebbles.",
    "snow": "Snow and frost ground.",
    "rock": "Natural rock surfaces for cliffs, outcrops, and boulders.",
    "stone": "Cut or dressed stone: flagstones, blocks, carved surfaces.",
    "masonry": "Walls of laid stone or brick with mortar.",
    "brick": "Fired brick.",
    "plaster": "Plaster, stucco, adobe, and render.",
    "wood": "Worked wood: planks, beams, boards.",
    "bark": "Tree bark and trunks.",
    "foliage": "Leaves, fronds, needles, vines, and flowers, usually cutout cards.",
    "roof": "Roofing: thatch, tiles, shingles.",
    "metal": "Metal surfaces.",
    "fabric": "Cloth, canvas, tents, banners, rugs.",
    "water": "Water surfaces.",
    "lava": "Lava and molten surfaces.",
    "ice": "Ice and crystal.",
    "trim": "Borders, edges, and ornamental strips.",
    "detail": "Small features: windows, doors, signs, carvings, decals.",
    "transition": "A blend between two surfaces, such as grass into dirt.",
    "normalMap": "A normal map paired with a diffuse texture.",
    "sky": "Sky, cloud, backdrop, and celestial images.",
    "effect": "Particle, spell, and glow images.",
    "other": "Anything the categories above do not cover; say what it is in the description.",
  },
  "model": {
    "tree": "Trees.",
    "shrub": "Bushes, small plants, grass clumps, flowers.",
    "rock": "Rocks, boulders, and outcrops.",
    "cliffPiece": "Large rock pieces that build cliffs, arches, and spires.",
    "building": "Whole buildings and building shells.",
    "buildingPiece": "Walls, roofs, stairs, columns, and other parts that build structures.",
    "fence": "Fences, railings, and barriers.",
    "bridge": "Bridges and walkways.",
    "furniture": "Tables, chairs, beds, shelves.",
    "container": "Barrels, crates, chests, sacks.",
    "lightSource": "Torches, lamps, braziers, lanterns, candles.",
    "decoration": "Statues, banners, signs, and ornaments.",
    "debris": "Rubble, bones, and scattered small things.",
    "water": "Water features: fountains, pools, falls.",
    "effect": "Models that carry effects or particles.",
    "other": "Anything the categories above do not cover; say what it is in the description.",
  },
  "light": {
    "torch": "Torches and small flames on walls or posts.",
    "brazier": "Braziers, fire pits, and campfires.",
    "lamp": "Lamps, lanterns, and candles.",
    "window": "Light spilling from windows and openings.",
    "fill": "Broad ambient fill that lights a room or area.",
    "magic": "Colored magical or crystal light.",
    "lava": "Glow from lava or heat.",
    "other": "Anything the categories above do not cover; say what it is in the description.",
  },
  "emitter": {
    "fire": "Flames.",
    "smoke": "Smoke and soot.",
    "steam": "Steam and vapor.",
    "dust": "Dust, motes, and floating particles.",
    "sparks": "Sparks and embers.",
    "water": "Splashes, spray, mist, and drips.",
    "weather": "Rain, snow, and wind-blown things.",
    "creatures": "Insects, birds, bats, and other small animated life.",
    "magic": "Glows, portals, and magical effects.",
    "other": "Anything the categories above do not cover; say what it is in the description.",
  },
  "ecosystem": {
    "grassland": "Grass and meadow ground.",
    "forest": "Forest floor.",
    "desert": "Sand and dry ground.",
    "rocky": "Rocky and mountain ground.",
    "swamp": "Wet and swampy ground.",
    "snow": "Snow and ice ground.",
    "farmland": "Fields and cultivated ground.",
    "path": "Roads and paths.",
    "shore": "Beaches and banks.",
    "other": "Anything the categories above do not cover; say what it is in the description.",
  },
}

tagGroups = {
  "material": {
    "meaning": "What the surface is made of.",
    "terms": {
      "sandstone": "Layered sedimentary rock, usually warm-colored.", "limestone": "Pale sedimentary rock.", "granite": "Speckled hard rock.",
      "marble": "Veined polished stone.", "slate": "Dark layered stone.", "basalt": "Dark volcanic rock.", "obsidian": "Black volcanic glass.",
      "clay": "Fired or raw clay.", "soil": "Earth and dirt.", "sand": "Sand.", "gravel": "Loose small stones.", "mud": "Wet earth.",
      "grass": "Grass.", "moss": "Moss and lichen.", "leaves": "Leaves and fronds.", "bark": "Tree bark.", "planks": "Sawn boards.",
      "logs": "Round timber.", "thatch": "Straw or reed roofing.", "cloth": "Woven fabric.", "leather": "Hide and leather.",
      "iron": "Iron and steel.", "bronze": "Bronze, brass, and copper.", "gold": "Gold.", "crystal": "Crystal and gems.", "bone": "Bone.",
      "ice": "Ice.", "snow": "Snow.", "water": "Water.", "lava": "Lava.", "plaster": "Plaster and stucco.", "brick": "Brick.",
      "tile": "Ceramic or stone tiles.", "glass": "Glass.",
    },
  },
  "color": {
    "meaning": "The dominant hues, as they read in the client.",
    "terms": {
      "red": "Red.", "orange": "Orange.", "yellow": "Yellow.", "tan": "Tan, buff, and sandy beige.", "brown": "Brown.", "green": "Green.",
      "teal": "Blue-green.", "blue": "Blue.", "purple": "Purple and violet.", "pink": "Pink.", "grey": "Grey.", "black": "Black and near black.",
      "white": "White and near white.", "multicolor": "Several strong hues.",
    },
  },
  "tone": {
    "meaning": "How the colors read overall.",
    "terms": {
      "warm": "Leans red and yellow.", "cool": "Leans blue and green.", "neutral": "Neither warm nor cool.", "dark": "Low brightness.",
      "light": "High brightness.", "saturated": "Strong color.", "muted": "Weak, greyed color.",
    },
  },
  "biome": {
    "meaning": "The kinds of place it belongs in.",
    "terms": {
      "desert": "Dry sand and rock lands.", "canyon": "Gorges, mesas, and layered cliffs.", "grassland": "Plains and meadows.",
      "forest": "Temperate woods.", "jungle": "Tropical growth.", "swamp": "Marsh and bog.", "tundra": "Cold open land.",
      "mountain": "High rocky land.", "coast": "Beaches and shores.", "cave": "Caves and caverns.", "underground": "Built spaces below ground.",
      "volcanic": "Lava and ash lands.", "urban": "Towns and cities.", "ruins": "Fallen and abandoned places.", "planar": "The planes and the otherworldly.",
      "underwater": "Below water.",
    },
  },
  "style": {
    "meaning": "The culture or craft it shows.",
    "terms": {
      "dwarven": "Dwarven stonework and metal.", "human": "Human towns.", "elven": "Elven grace.", "darkElven": "Teir'Dal style.",
      "gnomish": "Gnomish tinkering.", "ogre": "Ogre crudeness.", "troll": "Troll swamp style.", "iksar": "Iksar empire.", "vahShir": "Vah Shir.",
      "erudite": "Erudite marble.", "halfling": "Halfling homes.", "barbarian": "Northern barbarian.", "goblin": "Goblin and kobold crudeness.",
      "giant": "Giant scale.", "dragon": "Dragon lairs.", "primitive": "Rough and hand-made.", "rustic": "Simple country building.",
      "ornate": "Decorated and refined.", "ancient": "Old and weathered.", "magical": "Otherworldly and enchanted.",
    },
  },
  "use": {
    "meaning": "Where it goes on a zone.",
    "terms": {
      "terrainFlat": "Flat and gently sloping ground.", "terrainSlope": "Moderate slopes.", "cliffFace": "Steep and vertical natural faces.",
      "path": "Roads and paths.", "floor": "Built floors.", "wall": "Built walls.", "ceiling": "Ceilings and cave roofs.", "roof": "Roofs.",
      "trim": "Edges and borders.", "overlay": "Laid over another surface: decals, cracks, stains.", "cutoutCard": "Alpha-tested cards: foliage, fences.",
      "accent": "Small areas that draw the eye.", "water": "Water surfaces.", "sky": "The sky.",
    },
  },
  "pattern": {
    "meaning": "How the image is laid out.",
    "terms": {
      "tiling": "Repeats without visible seams.", "unique": "Meant to be seen once, not repeated.", "strata": "Horizontal layers or bands.",
      "grain": "A directional grain, as in wood.", "blocks": "Regular blocks or tiles.", "noisy": "Fine irregular detail.",
      "smooth": "Little detail.", "organic": "Irregular natural shapes.",
    },
  },
  "condition": {
    "meaning": "The state it is in.",
    "terms": {
      "clean": "Fresh and unmarked.", "worn": "Weathered and used.", "cracked": "Cracked and broken.", "mossy": "Overgrown with moss.",
      "dirty": "Stained and grimy.", "burnt": "Scorched.", "wet": "Wet and shiny.",
    },
  },
  "scale": {
    "meaning": "How big its features read at its usual repeat.",
    "terms": {"fine": "Small features: pebbles, grass blades.", "medium": "Hand-sized to body-sized features.", "coarse": "Large features: boulders, strata."},
  },
  "quality": {
    "meaning": "How well it holds up in a 2011-era zone.",
    "terms": {"good": "Holds up; use freely.", "usable": "Works with care: limited repeat, distance, or pairing.", "poor": "Dated, blurry, or flawed; avoid."},
  },
}


def merged(extensions):
  """The base vocabulary with terms added later: extensions is {"categories": {kind: {term: meaning}}, "tags": {group: {term: meaning}}}."""
  mergedCategories = {kind: dict(terms) | extensions.get("categories", {}).get(kind, {}) for kind, terms in categories.items()}
  mergedGroups = {group: {"meaning": entry["meaning"], "terms": dict(entry["terms"]) | extensions.get("tags", {}).get(group, {})} for group, entry in tagGroups.items()}
  return {"kinds": dict(kinds), "categories": mergedCategories, "tagGroups": mergedGroups}
