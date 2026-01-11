# Discord Bot Commands Documentation

Complete list of all available commands for the FIFA Targrean Discord Bot V2.

## Table of Contents
- [Points System](#points-system)
- [Attack & Defense](#attack--defense)
- [Predictions](#predictions)
- [Guild Wars](#guild-wars)
- [Auto-Reply](#auto-reply)
- [Role Shop](#role-shop)
- [Moderation](#moderation)
- [SOOP Notifications](#soop-notifications)
- [Spam Detection](#spam-detection)
- [Daily Quest](#daily-quest)

---

## Points System

### `/point`
**Description:** Check your current points and statistics
**Usage:** `/point`
**Output:** Displays your current points, daily earned progress (0/600), attack gains (0/2000), defense losses (0/2000), and stashed points (0/5000)
**Cooldown:** None
**Visibility:** Ephemeral (only you can see)

### `/checkpoints`
**Description:** Check points for a specific user
**Usage:** `/checkpoints @user`
**Parameters:**
- `user`: The user to check points for
**Visibility:** Ephemeral (only you can see)

### `/showtax`
**Description:** Display the current tax pool amount
**Usage:** `/showtax`
**Output:** Shows current tax pool and information about how tax is collected (5% from attacks, 10% from point transfers, 10% daily tax on rich users)
**Auto-Delete:** Message deletes after 10 seconds

### `/stash deposit`
**Description:** Deposit points into your stash for safekeeping
**Usage:** `/stash deposit amount:100`
**Parameters:**
- `amount`: Amount of points to deposit (minimum 1)
**Details:**
- Maximum stash capacity: 5000 points
- Stashed points are safe from attacks
- Earns 10% interest daily
- Cannot exceed 5000 total stashed points

### `/stash withdraw`
**Description:** Withdraw points from your stash
**Usage:** `/stash withdraw amount:100`
**Parameters:**
- `amount`: Amount of points to withdraw (minimum 1)
**Details:** Can only withdraw what you have stashed

### `/sendpoint`
**Description:** Send points to another user with 10% tax
**Usage:** `/sendpoint @user 100 "birthday gift"`
**Parameters:**
- `user`: The recipient
- `amount`: Points to send (positive number)
- `reason`: Reason for sending
**Details:**
- 10% tax is deducted (rounded down)
- Receiver gets 90% of amount
- 10% goes to tax pool
- Tracks total sent/received for leaderboards

### `/profile`
**Description:** Check your or another user's profile with detailed statistics
**Usage:** `/profile` or `/profile @user`
**Parameters:**
- `user`: (Optional) The user to check
**Output:** Shows:
- Total points, sent, received
- Daily earned progress (0/600)
- Attack/Defense stats (0/1000)
- Stashed points (0/5000)
- Attack win rates (≤100 pts and >100 pts)
- Profit breakdown (Attack, Defense, Prediction, Guild War, Beg, Trap, Dodge, Pierce)
- Active temporary roles with time remaining
- Active effects (Ceasefire status)
- Server join date

**Auto-Delete:** If used outside shop channel, deletes after 30 seconds

### `/attackhistory`
**Description:** Show who attacked you in the last 24 hours
**Usage:** `/attackhistory`
**Output:**
- List of recent attacks (up to 20)
- Attack type (regular, pierce, dodge)
- Success/failure status
- Points gained/lost per attack
- Time ago for each attack
- Summary statistics (total attacks, successful, failed, total points lost)
**Visibility:** Ephemeral (only you can see)

---

## Attack & Defense

### `/attack`
**Description:** Attack another user to steal points
**Usage:** `/attack @target 50`
**Parameters:**
- `target`: User to attack
- `amount`: Points to risk (50-250, default 50)
**Mechanics:**
- **Success Rate:** 45% for ≤100 points, 35% for >100 points
- **Rich Target Bonus:** +15% success when target has >3000 points
- **Tax:** 5% tax on all attacks (win or lose)
- **On Success:** Steal amount from target (minus 5% tax)
- **On Failure:** Lose amount to target (5% tax collected)
- **Dodge Interaction:** If target has active dodge, attack automatically fails and attacker loses 2x amount
- **Cooldown:** 20 seconds
**Restrictions:**
- Cannot attack yourself
- Cannot attack bots
- Cannot attack moderators
- Moderators cannot attack users
- Target must have enough points
- Target must not have lost 1000 points today
- Attacker must not have gained 1000 points today
**Ceasefire:** Breaking active ceasefire applies 10-minute debuff

### `/pierce`
**Description:** Pierce attack - 100% success vs dodge, 100% fail otherwise
**Usage:** `/pierce @target 100`
**Parameters:**
- `target`: User to attack
- `amount`: Points to risk (100-200, default 100)
**Mechanics:**
- **If target has dodge:** Guaranteed success, gain 10x amount (minus 5% tax), dodge is consumed
- **If target has no dodge:** Guaranteed fail, lose 1x amount (5% tax)
- **Cooldown:** 20 seconds (shared with regular attack)
**Restrictions:** Same as regular attack

### `/dodge`
**Description:** Activate dodge to block the next attack
**Usage:** `/dodge`
**Cost:** 50 points
**Effect:** Next attack within 5 minutes automatically fails. Attacker loses 2x the attack amount.
**Cooldown:** 15 minutes
**Details:**
- Dodge is consumed when triggered (even if not attacked)
- Not visible to others (strategic surprise)
- Can counter pierce attacks for massive gains (10x)
- Successfully dodging an attack counts toward your daily defense loss limit (1000 points/day)

### `/shutup`
**Description:** Timeout a user by sacrificing half your points
**Usage:** `/shutup <target> <text>`
**Condition:** You must have more points than the target
**Cost:** Half of your points (50% to tax pool, 50% to target)
**Effect:**
- You lose half of your points
- Half goes to tax pool, half goes to target
- Target is timed out for 3 minutes
- Custom message is displayed in the notification
**Restrictions:**
- Cannot target yourself
- Cannot target bots
- Cannot target moderators
- Moderators cannot use this command

### `/ceasefire`
**Description:** Activate ceasefire to prevent all attacks
**Usage:** `/ceasefire`
**Cost:** 50 points
**Effect:** Immune from all attacks for 15 minutes
**Cooldown:** 30 minutes
**Details:**
- Breaking your own ceasefire by attacking applies 10-minute debuff
- Notification sent when ceasefire is broken

### `/test_attack`
**Description:** Test attack simulation without actually changing points
**Usage:** `/test_attack @target 100`
**Parameters:**
- `target`: User to simulate attack against
- `amount`: Points to risk (50-250, default 50)
**Output:** Shows what would happen without affecting points or cooldowns
**Visibility:** Ephemeral (only you can see)

### `/trap`
**Description:** Set a trap with a trigger word that steals points
**Usage:** `/trap trigger:"hello world"`
**Parameters:**
- `trigger`: Text that will trigger the trap (5-50 characters)
**Cost:** 40 points
**Mechanics:**
- When someone types the trigger text, trap activates
- **If victim has ≥200 points:** Steal 200 points from victim
- **If victim has <200 points:** Add penalty role for 24 hours
- Trap creator is immune to their own trap
- Trap expires after 15 minutes if not triggered
- Only one trap per trigger per channel
**Cooldown:** 30 minutes

### `/beg`
**Description:** Create a beg request with custom title and message
**Usage:** `/beg`
**Opens Modal:** Input fields for beg title and message
**Details:**
- Posted to designated beg channel
- Others can check your points, give points, or attack you
- Attack mechanics:
  - 45% success for ≤100 points, 35% for >100 points
  - **Rich beggar (+20% attack chance):** If beggar has >1500 points
  - **Poor beggar (-20% attack chance):** If beggar has <500 points
  - Attack amount: 50-500 points
  - Cooldown: 60 seconds
  - 5% tax on successful attacks

---

## Predictions

### `/predict`
**Description:** Create a new prediction event
**Usage:** `/predict duration:30 choices:2 max_bet:500`
**Parameters:**
- `duration`: Duration in minutes (1-720)
- `choices`: Number of choices (2-4, default 2)
- `max_bet`: (Optional) Maximum bet amount per user
**Cost:** Configurable (default 200 points, free for mods)
**Opens Modal:** Input fields for title and choice texts
**Details:**
- Creates prediction in thread with buttons to bet
- Non-mod creators receive 60% of total tax collected when resolved
- 10% tax on winnings
- Mods can bet on their own predictions

### `/predlock`
**Description:** Force end betting early on a prediction
**Usage:** `/predlock prediction_id:1`
**Parameters:**
- `prediction_id`: Prediction ID to lock
**Permissions:** Creator or Moderator
**Details:** Stops all betting but doesn't resolve the prediction

### `/predresult`
**Description:** Resolve a prediction with a winner
**Usage:** `/predresult prediction_id:1 winner:2`
**Parameters:**
- `prediction_id`: Prediction ID to resolve
- `winner`: Winning choice number (1-4)
**Permissions:** Creator or Moderator
**Details:**
- Distributes winnings to winners based on bet share
- 10% tax on all winnings
- 60% of tax goes to non-mod creator
- Automatically archives thread

### `/predundo`
**Description:** Undo a prediction result to change winner
**Usage:** `/predundo prediction_id:1`
**Parameters:**
- `prediction_id`: Prediction ID to undo
**Permissions:** Moderator only
**Details:**
- Reverts all winnings
- Sets prediction back to locked status
- Can then use `/predresult` with correct winner

### `/predcancel`
**Description:** Cancel a prediction and refund all bets
**Usage:** `/predcancel prediction_id:1`
**Parameters:**
- `prediction_id`: Prediction ID to cancel
**Permissions:** Moderator only
**Details:**
- Refunds all bets to participants
- Refunds creation cost to non-mod creator
- Archives thread

### `/predcost`
**Description:** Set the cost to create a prediction
**Usage:** `/predcost cost:200`
**Parameters:**
- `cost`: New cost in points (minimum 0)
**Permissions:** Moderator only
**Details:** Free for moderators regardless of cost

### `/predictions`
**Description:** Show all active predictions
**Usage:** `/predictions`
**Output:** List of all betting/locked predictions with time remaining
**Visibility:** Ephemeral (only you can see)

---

## Guild Wars

### `/guildwar`
**Description:** Create a new guild war
**Usage:** `/guildwar`
**Opens Modal:** Input fields for war name, team names, and entry cost
**Parameters (in modal):**
- `war_name`: Name of the war
- `team1_name`: Team 1 name
- `team2_name`: Team 2 name
- `entry_cost`: Points required to join (minimum 10)
**Details:**
- Creates thread in guild war channel
- Players can buy potions (20% of entry cost each):
  - HP Potion: +20 HP per potion (max 3)
  - ATK Potion: +5 ATK per potion (max 3)
- War mechanics:
  - Base HP: 100 + potion bonuses
  - Base Damage: 30 + ATK potion bonuses
  - Status effects: Dodge (75% evasion), Power Up (+20 dmg), Shield (-40% dmg), Focus (+20% crit), Heal (+25 HP), Berserk (+25 dmg +25% vulnerable), Weaken (-10 dmg), Vulnerable (+30% dmg taken), Stun (skip turn)
  - Perfect Strike: 1% chance for instant KO
  - Defense mechanic: Defender takes 20% damage, attacker takes 80%
  - Winner gets share of prize pool (5% tax)

### `/startwar`
**Description:** Start the guild war battle simulation
**Usage:** `/startwar war_id:1`
**Parameters:**
- `war_id`: War ID to start
**Permissions:** War Creator or Moderator
**Requirements:** Both teams need at least 1 member
**Details:**
- Automated battle simulation with rounds
- Status effects and combat mechanics
- Sudden death if both teams eliminated
- 5% tax, prize distributed to winners

### `/cancelwar`
**Description:** Cancel the guild war and refund all participants
**Usage:** `/cancelwar war_id:1`
**Parameters:**
- `war_id`: War ID to cancel
**Permissions:** War Creator or Moderator
**Details:**
- Refunds all entry costs
- Cannot cancel finished wars
- Archives and locks thread

---

## Auto-Reply

### `/autoreply`
**Description:** Set auto-reply for a user in a channel
**Usage:** `/autoreply @user #channel "stop spamming"`
**Parameters:**
- `user`: Target user to auto-reply to
- `channel`: Channel to auto-reply in
- `message`: Auto-reply message text (max 500 chars)
**Cost:** Configurable (default 200 points, free for mods/admins)
**Duration:** Configurable (default 2 minutes)
**Details:**
- Max 3 active auto-replies server-wide
- Max 5 replies per auto-reply
- One auto-reply per creator at a time
- One auto-reply per target at a time
- Auto-expires after duration

### `/autoreplystop`
**Description:** Stop auto-reply for a user
**Usage:** `/autoreplystop @user #channel`
**Parameters:**
- `user`: Target user
- `channel`: Channel
**Cost:**
- Free for mods/admins
- Free for creator
- 1.5x cost for others
**Details:** Immediately stops the auto-reply

### `/autoreplylist`
**Description:** Show all active auto-replies
**Usage:** `/autoreplylist`
**Permissions:** Moderator or Administrator
**Output:** List of all active auto-replies with target, time left, and message preview
**Visibility:** Ephemeral (only you can see)

### `/autoreplycost`
**Description:** Set the cost for auto-reply
**Usage:** `/autoreplycost cost:200`
**Parameters:**
- `cost`: New cost in points (minimum 0)
**Permissions:** Moderator only
**Details:** Always free for moderators

### `/autoreplyduration`
**Description:** Set the duration for auto-reply
**Usage:** `/autoreplyduration minutes:5`
**Parameters:**
- `minutes`: Duration in minutes (1-60)
**Permissions:** Moderator only

---

## Role Shop

### `/shop`
**Description:** Show all available roles and prices
**Usage:** `/shop`
**Output:** Posts role shop listing in designated shop channel
**Details:**
- Shows all purchasable roles with prices
- Split into pages if more than 10 roles
- Includes role duration information

### `/buyrole`
**Description:** Purchase a role for yourself or another user
**Usage:** `/buyrole role:"VIP" @target`
**Parameters:**
- `role`: Role name (autocomplete from shop)
- `target`: (Optional) User to give role to (default: yourself)
**Details:**
- Deducts points from buyer
- Adds role to target with expiration timer
- Cannot buy for moderators
- Role auto-removes when expired
- Duration set by config (default varies)

### `/shopadd`
**Description:** Add a role to the shop
**Usage:** `/shopadd @role 500`
**Parameters:**
- `role`: Role to add
- `price`: Price in points (minimum 1)
**Permissions:** Moderator only
**Details:**
- Automatically assigns 1440 minute (24 hour) duration to all existing role holders
- Updates price if role already in shop

### `/shopremove`
**Description:** Remove a role from the shop
**Usage:** `/shopremove @role`
**Parameters:**
- `role`: Role to remove
**Permissions:** Moderator only
**Details:** Only removes from shop database, doesn't affect existing role holders

### `/shopprice`
**Description:** Change the price of a role in the shop
**Usage:** `/shopprice @role 300`
**Parameters:**
- `role`: Role to update
- `price`: New price in points (minimum 1)
**Permissions:** Moderator only
**Details:** Role must already be in shop

---

## Moderation

### `/addpoint`
**Description:** Add points to a user
**Usage:** `/addpoint @user 1000`
**Parameters:**
- `user`: User to add points to
- `amount`: Amount to add (minimum 1)
**Permissions:** Moderator only
**Notification:** Sent to bot channel

### `/removepoint`
**Description:** Remove points from a user
**Usage:** `/removepoint @user 500`
**Parameters:**
- `user`: User to remove points from
- `amount`: Amount to remove (minimum 1)
**Permissions:** Moderator only
**Details:** Won't go below 0 points
**Notification:** Sent to bot channel

### `/taxairdrop`
**Description:** Distribute tax pool to all users
**Usage:** `/taxairdrop percentage:100`
**Parameters:**
- `percentage`: Percentage of tax pool to distribute (1-100, default 100)
**Permissions:** Moderator only
**Details:**
- Distributes equally to all users with points >0
- Shows per-user amount and total recipients

### `/airdrop`
**Description:** Start an airdrop for users to claim points
**Usage:** `/airdrop amount:100 max_users:5`
**Parameters:**
- `amount`: Points to give per claim (1-10000, default 100)
- `max_users`: Max users who can claim (1-100, default 5)
**Permissions:** Moderator only
**Mechanics:**
- React with 💸 to claim
- **Poor player (<500 points):** 50% chance for 2x (critical)
- **Rich player (>1500 points):** 70% chance for nothing (bad luck)
- First X users can claim
- Auto-updates with claim count

### `/pointanalysis`
**Description:** Show point statistics and distribution
**Usage:** `/pointanalysis`
**Permissions:** Moderator only
**Output:**
- Mean, Q1, Median (Q2), Q3 statistics
- Distribution histogram by 500-point bins
- Shows percentage and count per bin

### `/leaderboard`
**Description:** Show top 10 leaderboard
**Usage:** `/leaderboard`
**Output:** Top 10 users by points (excludes moderators)
**Visibility:** Ephemeral (only you can see)

### `/transfers`
**Description:** Show top 10 senders and receivers
**Usage:** `/transfers`
**Output:** Top 10 users who sent most points and top 10 who received most
**Visibility:** Ephemeral (only you can see)

---

## SOOP Notifications

### `/sooplist`
**Description:** Show monitored SOOP streamers
**Usage:** `/sooplist`
**Output:** List of all streamers being monitored with their target channels
**Visibility:** Ephemeral (only you can see)

### `/soopadd`
**Description:** Add a streamer to monitor
**Usage:** `/soopadd username:"fifatargrean" #channel`
**Parameters:**
- `username`: SOOP username to monitor
- `channel`: Channel to send notifications
**Permissions:** Moderator only
**Details:**
- Checks stream status every 4 minutes
- Sends notification when streamer goes live
- Prevents duplicate notifications for same stream

### `/soopremove`
**Description:** Remove a streamer from monitoring
**Usage:** `/soopremove username:"fifatargrean"`
**Parameters:**
- `username`: SOOP username to stop monitoring
**Permissions:** Moderator only

---

## Spam Detection

### `/spamunban`
**Description:** Unban a user banned by spam detector
**Usage:** `/spamunban user_id:"123456789"`
**Parameters:**
- `user_id`: User ID to unban
**Permissions:** Moderator only
**Details:**
- Used to reverse auto-bans from spam detector
- Notifies bot channel

### `/spamsetmod`
**Description:** Set the mod notification channel for spam alerts
**Usage:** `/spamsetmod #channel`
**Parameters:**
- `channel`: Channel to send spam detection alerts
**Permissions:** Moderator only

### `/spamignore`
**Description:** Add a channel to spam detector ignore list
**Usage:** `/spamignore #channel`
**Parameters:**
- `channel`: Channel to ignore for spam detection
**Permissions:** Moderator only
**Details:** Links in this channel won't trigger spam detection

### `/spamunignore`
**Description:** Remove a channel from spam detector ignore list
**Usage:** `/spamunignore #channel`
**Parameters:**
- `channel`: Channel to stop ignoring
**Permissions:** Moderator only

**Spam Detection Mechanics:**
- Detects links in messages
- Triggers when user posts links in 4+ different channels within 30 seconds
- Auto-bans user and deletes last 30 minutes of messages
- Sends notifications to mod channel, bot channel, and guild owner
- Moderators and admins are immune (test mode)

---

## Daily Quest

### `/daily`
**Description:** Claim your daily reward
**Usage:** `/daily`
**Reward:** 100 points
**Cooldown:** Once per day (resets at midnight)
**Details:** Can only claim once per day

---

## Passive Point Earning

### Chatting
- **First message of day:** 100 points
- **Regular messages:** 10 points per message
- **Cooldown:** 15 seconds between messages
- **Daily Cap:** 600 points per day from chatting
- **Luck Mechanics:**
  - **Poor (<500 points):** 20% chance for critical (2x points)
  - **Rich (>1500 points):** 50% chance for bad luck (0 points)
- **Server Booster Bonus:** 50% chance for extra 0.5x multiplier (1.5x total)

### New Member Bonus
- **First-time member:** 1000 points welcome gift (doesn't count toward daily cap)

### Daily Automated Tasks
- **Tax Collection (>3000 points):** 10% daily tax on rich users, collected to tax pool
- **Stash Interest:** 10% interest on stashed points (capped at 5000 max)
- **Reset:** Cumulative attack gains and defense losses reset to 0 daily

---

## Limitations & Caps

### Daily Caps
- **Chat Earning:** 600 points/day
- **Attack Gains:** 1000 points/day (cumulative from winning attacks)
- **Defense Losses:** 1000 points/day (cumulative from being attacked, including successful dodges)

### Point Limits
- **Stash:** Max 5000 points
- **Attack Amount:** 50-250 points per attack
- **Beg Attack Amount:** 50-500 points per attack

### Cooldowns
- **Attack:** 20 seconds
- **Beg Attack:** 60 seconds
- **Trap:** 30 minutes
- **Dodge:** 15 minutes
- **Ceasefire:** 30 minutes
- **Daily Quest:** 24 hours

### Restrictions
- Cannot attack yourself
- Cannot attack bots
- Cannot attack moderators (moderators also cannot attack)
- Cannot attack users who lost 1000 points today
- Cannot gain more than 1000 points from attacks today

---

## Special Notes

### Tax System
- **5% tax:** Attack wins, attack losses, beg attacks
- **10% tax:** Point transfers, prediction winnings, daily rich tax (>3000 points)
- **Tax pool:** Accumulated from all taxes, can be distributed by mods via `/taxairdrop`

### Moderator Privileges
- Free predictions, auto-replies
- Can attack/bet on own predictions
- Cannot be attacked
- Cannot attack others
- Bypass daily caps (implied)
- Access to all mod commands

### Role System
- Temporary roles expire automatically
- Database tracks expiration times
- Notifications sent when roles expire
- Roles removed automatically by bot when expired

### Thread Management
- Predictions create threads automatically
- Guild wars create threads automatically
- Threads archived automatically when finished/cancelled
- Threads can be unarchived by commands when needed

---

## Configuration Variables

Bot behavior is configured via database settings and can be modified by moderators:
- `prediction_cost`: Cost to create predictions (default 200, free for mods)
- `autoreply_cost`: Cost to create auto-replies (default 200, free for mods)
- `autoreply_duration`: Duration of auto-replies in minutes (default 2)
- `tax_pool`: Current tax pool amount
- Various channel IDs and role IDs in `Config` class

---

*This documentation is accurate as of January 5, 2026.*
