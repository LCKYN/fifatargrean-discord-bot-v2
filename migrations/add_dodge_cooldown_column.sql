-- Add dodge cooldown tracking column to users table
ALTER TABLE users
ADD COLUMN IF NOT EXISTS dodge_cooldown_at TIMESTAMP;
