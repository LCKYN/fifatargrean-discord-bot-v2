-- Add potion tracking columns to guild_wars table
ALTER TABLE guild_wars
ADD COLUMN IF NOT EXISTS team1_hp_potions INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS team1_atk_potions INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS team2_hp_potions INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS team2_atk_potions INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS potion_message_id BIGINT;
