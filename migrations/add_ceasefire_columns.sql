-- Add ceasefire tracking columns to users table
ALTER TABLE users
ADD COLUMN IF NOT EXISTS ceasefire_activated_at TIMESTAMP;

ALTER TABLE users
ADD COLUMN IF NOT EXISTS ceasefire_duration INTEGER;
