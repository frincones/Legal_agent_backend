-- Case State: persistent accumulated case context per session
CREATE TABLE IF NOT EXISTS case_states (
    session_id VARCHAR(200) PRIMARY KEY,
    state JSONB NOT NULL DEFAULT '{}',
    turn_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Attachments: files attached to chat sessions
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS
    attachments JSONB DEFAULT '[]';
