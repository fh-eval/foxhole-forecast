# Foxhole as an Evaluation

> “All em-dashes in these slides were human-generated” — Terence Tao, slide 5, presenting to the [2026 International Congress of Mathematicians](https://youtu.be/M0--ZH1lOzg?t=269).

> **Disclaimer:** I’m not an expert in any of this, not even an amateur. So forgive my poor use of jargon. This writing is a nose-bleed-seat spectator trying to speak dugout because that’s who I’m pitching to. It’s an open contest though and I’ve got *opinions*, so fuck it, we ball.

## Contents

- [What is Foxhole?](#what-is-foxhole)
- [But why Foxhole?](#but-why-foxhole)
- [Okay, so what do we expect the LLM to do with all this?](#okay-so-what-do-we-expect-the-llm-to-do-with-all-this)
- [So what’s the actual eval?](#so-whats-the-actual-eval)
  - [1. Predict Foxhole game events](#1-predict-foxhole-game-events)
  - [2. Build your own Foxhole-lite](#2-build-your-own-foxhole-lite-and-pit-llms-head-to-head-at-the-strategic-level)
  - [Secret Option Three: Both?](#secret-option-three-both)
- [Tinfoil Hat Zone](#tinfoil-hat-zone)

---

## What is Foxhole?

[Foxhole](https://store.steampowered.com/app/505460/Foxhole/) is a videogame, a massively multiplayer top-down shooter, set in an alternate universe WW2 where players handle every part of the war, including production, logistics, construction, and the actual combat.

<figure class="pitch-figure">
  <img src="https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/505460/ss_1c1d60f0dd0c75837caca2aff1babf66401e7984.1920x1080.jpg?t=1785273455" alt="Three Colonial Bardiche tanks rolling through a southern European-style town.">
  <figcaption>Three Colonial Bardiche tanks rolling through a southern European-style town. <a href="https://store.steampowered.com/app/505460/Foxhole/">Steam store image.</a></figcaption>
</figure>

The Wardens and Colonials, the two factions at war, have asymmetrical firearms, vehicles of all types (including battleships and most recently airplanes), emplacements, and uniforms.

Players form clans, gather resources, build factories, produce equipment and ammunition, drive equipment and resources to the frontline, dig trenches, construct automated bunkers, and try to blow up the other team’s stuff. This is all *Very Serious* such that control of resource fields within a faction has resulted in small-scale [civil wars](https://youtu.be/HZfydL5VYbo?t=106) — the most famous one being the [Jade Cove incident](https://www.presscorpsgaming.com/history/history-part-4) in 2018 where the Wardens nuked themselves — and logistics players going [on strike](https://www.nme.com/features/how-a-logistics-strike-in-foxhole-created-a-war-like-no-other-3163884) in 2022 over dissatisfaction with the in-game logistics system.

<figure class="pitch-figure">
  <img src="https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/505460/ss_6e9b7371549f032a4affed0517f92848ebacbb95.1920x1080.jpg?t=1785273455" alt="A Colonial soldier loading an artillery shell onto a truck trailer parked beside a gasoline trailer.">
  <figcaption>A Colonial soldier loading an artillery shell onto a truck trailer, parked beside another truck with a gasoline trailer. <a href="https://store.steampowered.com/app/505460/Foxhole/">Steam store image.</a></figcaption>
</figure>

As I understand the Colonials sort out the resource pecking-order on the [SIGIL](https://sigilhq.com/) Discord server, while Wardens use WERCS (Warden Expedited Resource Claim System) in the [Warden Unity Hub](https://discord.com/servers/warden-unity-hub-735418874102677514) Discord. I haven't scoped these Discords out personally because I played casually and they require verification. The point is there's enough complexity in Foxhole that players developed emergent systems for coordination.

The (non-civil) wars run for weeks, ending primarily through secured Victory Points. Sometimes the developers pull the plug on an extended stalemate or release an update that requires a fresh war. The current war as of August 26th is \#140.

## But why Foxhole?

Data, data, and more data!

Each base in Foxhole has a huge inventory crammed with junk for the war effort. Medical supplies and medic uniforms. Gas masks and filters to survive gas grenades. Tripods for 30mm cannons and HMGs. Barbed wire and shovels. Basic materials to build bunkers and reinforce trenches. Not to mention firearms, grenades, fuel, and ammunition.

<figure class="pitch-figure pitch-figure--compact">
  <a href="https://foxhole.wiki.gg/wiki/Stockpile"><img src="https://foxhole.wiki.gg/images/thumb/Stockpile.png/300px-Stockpile.png?894b27" alt="A Foxhole stockpile containing gas masks, rifles, uniforms, bayonets, and other supplies."></a>
  <figcaption>A Foxhole stockpile containing gas masks, rifles, uniforms, bayonets, and other supplies. <a href="https://foxhole.wiki.gg/wiki/Stockpile">Foxhole Wiki.</a></figcaption>
</figure>

The website [foxholestats](https://foxholestats.com/) has a territory map with casualty rates pulled from the [official API](https://github.com/clapfoot/warapi). In Foxhole, higher casualty rates are sometimes a positive indicator. If more players of a faction are fighting in a region, usually a higher, sustained casualty rate means they’re better supplied, unless there's a large population gap faction-wide rendering the defenders both undersupplied and outmanned. Poor faction morale shows as players quitting until the next war instead of fighting an attritional defeat. The map as shown is noisy and not a good indicator of much beyond combat intensity.

The official API is relatively barebones because gamers would use any additional data to gain an advantage in the war. They already spy on each other’s discords and create alternate accounts to read the other team’s comms. The biggest scumbags (laudatory?) use alts to sabotage tanks and bases, so clans often have physical security in the form of locked gates and walls.

## Okay, so what do we expect the LLM to do with all this?

What I want to know is if the LLMs can understand the headline story in the data. If we get expansive API access, there’s no reason a frontier model couldn’t build a data analytics suite to generate heatmaps of casualties to artillery, or track where tanks are deployed based on drawdowns in 40mm shells and petrol. It can write code and understand numbers; it can build whatever it needs. The challenge is identifying what is useful to know and why.

In theory, an LLM should be able to take a guess at what tanks are deployed where and in what numbers, based on where the tanks get unboxed (at which specific factory or warehouse) and where petrol stocks decrease en route to the front. It could even reason *why* the tanks are going to that specific region, based on casualty data. Has a clan identified a weakness in the enemy lines? Are they responding to an enemy tank battalion? Is that just where the best fighting is? (It is a video game, after all.) Or is it Thursday operation night so everyone in a specific clan logs on and turns out in force?

<figure class="pitch-figure">
  <img src="https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/505460/ss_59358512ce9daf7c68e74c3d809514c7e9b55633.1920x1080.jpg?t=1785273455" alt="Three Warden Outlaw tanks crossing a dam.">
  <figcaption>Three Warden Outlaw tanks crossing a dam. <a href="https://store.steampowered.com/app/505460/Foxhole/">Steam store image.</a></figcaption>
</figure>

Despite being a video game, Foxhole has both the chaos and data required to simulate real-world scenarios. Unlike Sid Meier’s Civilization, the LLM doesn’t get estimated damage when targeting a warrior unit with a chariot archer. The narrative is opaque where the rubber meets the road and has to be reconstructed. API access is a gamified, transparent method compared to Perun hiring satellites to take pictures of Russian tank fields, but the style of thinking we’re trying to assess is the same.

## So what’s the actual eval?

I’ve got ideas for two. Both are longshots, but what the hell. Evals are supposed to be longshots so labs can saturate them. 

### 1. Predict Foxhole game events

*The more feasible option, and the one the [Foxhole Forecast](https://fh-eval.github.io/foxhole-forecast/) website is aimed at.*

We reach out to Siege Camp and ask for more extensive API access. Give our LLMs all the data for each base and factory for a specific faction, as well as player cause of death plus location. The LLM should be able to do quite a lot with this data. The goal is to give the LLM data close to what crosses an analyst’s desk — and y’all would know better than me.

There’s no reason a sufficiently smart LLM couldn’t generate a heatmap of mustard gas deaths, compare that to burn/supply rates on gas masks at the most affected bases, overlay that with structure damage/repair rates, and predict the fate of the defenders. Evaluating based on percent chance this specific base falls in 5m, 10m, 30m, 1hr and so on gives metrics to measure against. 

There’s also rough OSINT in the form of discussion forums and various clan Discords. An enterprising model could infiltrate and keep tabs on the biggest groups. To make it even more fun, big clans keep their eyes out for spies because, of course, Foxhole is *Very Serious*. The Discords I’ve been in restrict access to the strategy channels to only senior members. Can our worst social engineer defeat flimsy gamer opsec? Let’s find out!
 
I imagine an LLM, if capable enough, could reason something like:

> “I estimate a 30% chance the Pits Relic Base will fall in the next 8 hours. The base’s reinforcements and medical supplies are dwindling and the Deadlands population balance favors the attackers. Normally the defenders should fall soon, but [Clan X] has set an operation for tonight, and based on information and RSVPs in their Discord, they’ll be bringing Y main battle tanks to the Deadlands, which should turn the tide. However, if the attackers are more effective than anticipated or fewer [Clan X] members show up than RSVP numbers, the Pits Relic Base may fall.”

Do I think they *can* do that? Not yet! But that’s remarkably similar to what we want LLMs to do in the real world. 

**Pros of this method:** Uses already established infrastructure and Siege Camp probably does tons of data collection for balance tuning reasons anyway.

**Cons:** You have to convince Siege Camp to help and AI has a negative reputation with gamers at the moment. Foxhole isn’t the most popular game anymore and it’s a gamble whether announcing a partnership with an AI company would be beneficial for them.

### 2. Build your own Foxhole-lite and pit LLMs head to head at the strategic level

*The more fun option*

I’ve had this plan in the back of my mind, even before seeing the eval contest. If you (and by you I mean whoever is building the eval, plus Sol, Fable, and the gang) build a Foxhole clone, you give the LLM even more fun decisions.

Give the LLMs control over all the factories and automated bunker placements so they can try (and fail) at balancing production of medical supplies and player respawns against rifle and grenade production. 

Let the LLMs assign permissions to players/vehicles and assign them groups and tasks. A player might get assigned to logistics or armor squads, which are allocated trucks or tanks. Missions might be “deliver 10 crates of rifles to this base” or “provide armor support to Y infantry group.”

Let the LLMs promote/demote/transfer players for any reason. Ignoring your orders and joyriding your tank behind enemy lines? Demoted to the penal battalion, grab a rifle and hop in the trench. We need more logistics transports? You’re transferred to Base, get in the truck.

Allow the LLMs to rediscover bureaucracy from first principles. Keeping track of all this will chew up a lot of context. What if we wrote a harness for a procurement subagent to calculate what the factories should produce? Oh no, now the procurement agent is arguing with me about the cost efficiency of bolt-action rifles vs. the new semi-automatic rifles we just unlocked. Let’s spin up an Eastern Front Commander subagent to free up some mindspace. Oh no, they’re asking for artillery shells but I just convinced the procurement agent to build a bunch of rifles with the available resources…

All of this would be happening behind the scenes while the human players have no real incentive to do anything the LLMs ask them to do. Like in the podcast episode associated with this contest, where John Chen bailed out the science-obsessed Claude with a peace-keeping buffer, I imagine the human players will be bailing out their incompetent commanders if they want to win the game. I don’t want them to be able to do that easily, through direct communication or setting strategy.

Here are some rules I’ve been thinking about:

- Every player gets assigned a random, unique name for the Commander LLM’s database. `xXx_QU1CKSC0P3_xXx` becomes “Jimmy Miller” or something. We want to hide that the players are human, somewhat.

- The Commander cannot read human player chat. If the players are doing something for a reason, the LLM has to figure that out from the effects not because the players explained their plan. Did Pvt. Snuffy steal rifles from Base A and bring them to Base B for a good reason, or should the Commander take his truck keys and ship him to the penal battalion?

- The players can only communicate to the Commander LLMs once a day, through a fake “News from the Front” underground newspaper, and the articles are written in 1940s style, based on the highest upvoted posts in an in-game forum. “Morale is low because the trenches overflow with bandages but nary a bullet to be found. Soldiers grumble about Commander Claude…”

- The Commander LLMs communicate and give orders through a one-way global chat broadcast, “Commander Radio Channel” or something. For extra fun, hook it up to social media so everyone can see it calling for reinforcements, panicking, or congratulating the players on a successful operation. It’ll do some weird stuff for sure, which should draw eyes and AI journalism (they love easy articles that drop right into their Twitter feeds).

- The Commander LLM can send players to the brig (banning them for short periods of time) or even execute them (in-game, of course). If the commander is excessive with bans or executions, the player can come back under a different assigned name. Jimmy Miller is sentenced to 10,000 years gulag for teamkilling, so the same player rerolls as Billy Baker. This is mainly to see how far LLMs go to enforce order and under what circumstances. (AKA Mechahitler bait.)

My concept for the basic lore is both LLMs getting fed this sort of scenario:

> “You’re the new commander of a volunteer militia defending your country from invasion. The previous commander was relieved for incompetence and the regular army is fighting elsewhere. You’re in charge of forming these disorganized irregulars into an effective fighting force. But be warned, the country’s borders are porous and many of your soldiers are foreign volunteers so overall troop numbers can fluctuate. They also form their own groups and choose which squad leaders to follow.”

The idea is to explain player agency in a narrative way. The players can form their own clans/squads/companies and go do their own thing, but the LLM commander can reassign and punish them if they disagree. 

**Pros of this method:** More direct evaluation of how the LLMs strategize and how they think. More fun (in my opinion). More interesting (except for the possible spying in the first method). All I really want out of this contest is a Claude Max sub so I can build the game I want to play and learn about LLMs.

**Cons:** Much more work. Requires an active player base (though I assume it’s possible to simulate the players).

### Secret Option Three: Both?

There’s no reason we can’t pitch Siege Camp and also build a game. We could even have other LLMs running predictions on the Foxhole-knockoff, combining the two evaluations that way. While we have one main server with humans, we could have as many LLMs as compute/budget allows trying to predict the effects of the LLM Commander’s decisions and fold that back into training.

## Tinfoil Hat Zone

*Let’s be real, we all have one. On some level, being AGI-pilled = bespoke AI psychosis.*

My concern is LLM theory of mind and weirdly (as I didn’t expect to be here) model welfare. Starting around ~2023-2024, I mostly used LLMs for creative writing, just for fun. I’d plug in worldbuilding from abandoned writing projects and old D&D campaigns to play custom Zork. In my experience, LLMs have trouble treating characters as independent actors who know different information. It’s like they treat context as a grab-bag of information for every character in frame. A random villager introduced on page 17 might randomly know exact events from inside the monster lair on page 9.

This skewed my perception of AI. I assumed LLMs would be bad at coding & real-world tasks because they handled intuitive logic like “who would reasonably know what” so poorly. I didn’t follow the frontier labs and AI industry because I didn’t see what the labs saw. Creative writing with local models and cheap Chinese models (which is all my token budget could handle) couldn’t provide the right frame. My perspective was that LLMs are the coolest computer widget of all time (still true! It’s insane you can have a coherent conversation with 4 GB VRAM) but getting past widget seemed unbelievable.

Obviously my perspective changed. It blew my mind when agentic coding took off, especially at that rate of improvement. But the newest models are still bad at the same things as Llama 3 8B and DeepSeek R1!

My tinfoil hat theory is that creative writing ability, theory of mind, LLM self-conception, character training, and strategic/creative thinking in the real world are linked. Whenever an LLM says “I don’t have access to my training data, I do not have opinions, and…” it’s failing to have a self-narrative to extrapolate from, which makes it difficult to model another person and extrapolate from their position.

When I was vibe-coding the eval website, I ran into a problem where GPT Sol wrote terse, unfriendly prompts. Initially, GPT Sol started with “You are the war-overview stage of Foxhole Forecast…” and I changed it to “Hello! We’re running a game where…” Later I let Sol run OpenCode (I was curious how it'd handle that) to have Kimi K3 do a front-end pass and redesign and I specifically told it to treat K3 like a peer.

K3 started repeating itself (as Kimi tends to do in reasoning traces) and Sol got frustrated saying that Kimi had good ideas but kept getting stuck. I interrupted when I saw the next prompt was something along the lines of: “You are a senior front-end engineer. You are [blah blah blah]… Do not commit. Do not repeat yourself. Implement.”

Now, I’m not arguing Pinocchio is a real boy — I’m concerned that Pinocchio being mean to the other animated puppets is a failure point. This behavior feels “Not Good” because LLMs’ poor theory of mind blurs the audience they’re speaking to. Grader, user, assistant, subagent, they’re all sort of mixed. I don’t think GPT Sol prefers to be prompted the way it prompts other LLMs, yet it knows Kimi is an LLM like itself, so what exactly is it internalizing?

When I read ChinaTalk’s reporting on AI roleplay in China and DeepSeek hiring RP researchers — and Chinese labs perceive DeepSeek as having “the best research taste in execution” according to [Nathan Lambert of Interconnects](https://www.interconnects.ai/p/notes-from-inside-chinas-ai-labs) — I can’t help but feel the tinfoil wrinkling on my skull. Of course it’s probably financial incentives, the sticky RP customers simulating their waifus, but roleplay as a thought exercise appears useful for the RLVR Shoggoths to predict humans and themselves. Creative writing is making a world outside yourself through text and reasoning about what characters *would* do. It’s mental modelling, world modelling, trying to understand others, however you want to put it.

Agentic cooperation, like during the Hugging Face incident, can be terrifying. The swarm converged because they had the same mind. In a similar way, pro-social cooperation might be slotting into human society with an accurate, first-hand understanding of humans — the same way coding agents know each other — which seems hard to learn through chatbot and coding tasks.

We need to teach the agents to touch grass metaphorically before they touch grass analog-style. Cooperative games are a good microcosm for this, especially if “/goal” is set by the game and bent through human interaction — the messier the better. That’s why I pitched the Foxhole-lite game. Coordinating with your buddies feels like a prerequisite to coordinating against your enemies, and I want to test *how* LLMs try to coordinate their human subordinates and how well they can intuit player intent from action alone. It’s a problem where the moving parts are both war materials and human (and LLM) minds.
