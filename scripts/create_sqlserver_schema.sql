-- ============================================================
-- AI Meeting Intelligence System
-- SQL Server Schema — create_sqlserver_schema.sql
--
-- Run this script once in SSMS (connected to ICS-LT-H3J9R73\SQLEXPRESS)
-- before starting the application for the first time.
--
-- Requirements:
--   • SQL Server Express 2016+ (or any edition)
--   • Run as a login with db_owner or CREATE DATABASE permission
-- ============================================================


-- ────────────────────────────────────────────────────────────
-- Step 1: Create the database (skip if already exists)
-- ────────────────────────────────────────────────────────────

IF NOT EXISTS (
    SELECT 1 FROM sys.databases WHERE name = N'MeetingSystem'
)
BEGIN
    CREATE DATABASE MeetingSystem
    COLLATE SQL_Latin1_General_CP1_CI_AS;
    PRINT 'Database MeetingSystem created.';
END
ELSE
BEGIN
    PRINT 'Database MeetingSystem already exists — skipped.';
END
GO

USE MeetingSystem;
GO


-- ────────────────────────────────────────────────────────────
-- Step 2: Create the meetings table
-- ────────────────────────────────────────────────────────────

IF NOT EXISTS (
    SELECT 1 FROM sys.tables
    WHERE name = N'meetings' AND schema_id = SCHEMA_ID(N'dbo')
)
BEGIN
    CREATE TABLE dbo.meetings (

        -- ── Identity & file identity ─────────────────────────
        id                  INT             IDENTITY(1,1)       NOT NULL,
        file_path           NVARCHAR(450)                       NOT NULL,   -- 450 chars × 2 bytes = 900 bytes (within the 1700-byte nonclustered index key limit)
        file_name           NVARCHAR(260)                       NOT NULL,   -- MAX_PATH on Windows
        file_type           NVARCHAR(20)                        NOT NULL,   -- .vtt, .csv, etc.
        file_hash           NVARCHAR(64)                        NULL,       -- SHA-256 hex (64 chars)

        -- ── Timestamps ───────────────────────────────────────
        processed_at        DATETIME2(0)    DEFAULT GETUTCDATE() NOT NULL,  -- precision 0 = seconds; stored as UTC

        -- ── Large text fields (paged LOB storage) ────────────
        transcript          NVARCHAR(MAX)                       NULL,       -- Raw .vtt transcript
        mom_json            NVARCHAR(MAX)                       NULL,       -- LLM-generated MOM JSON
        attendance_json     NVARCHAR(MAX)                       NULL,       -- Attendance classification JSON
        sentiment_json      NVARCHAR(MAX)                       NULL,       -- Feature 8: morale analysis JSON

        -- ── Meeting metadata ─────────────────────────────────
        meeting_date        DATE                                NULL,       -- YYYY-MM-DD from MOM
        meeting_title       NVARCHAR(500)                       NULL,       -- Extracted meeting title

        -- ── Processing state ─────────────────────────────────
        status              NVARCHAR(30)    DEFAULT N'pending'  NOT NULL,
        error_message       NVARCHAR(2000)                      NULL,

        -- ── Delivery flags (BIT replaces SQLite INTEGER 0/1) ─
        teams_notified      BIT             DEFAULT 0           NOT NULL,
        email_sent          BIT             DEFAULT 0           NOT NULL,

        -- ── Feature 9: approval gate ─────────────────────────
        approval_token      NVARCHAR(64)                        NULL,       -- secrets.token_urlsafe(32) = 43 chars
        approval_status     NVARCHAR(30)    DEFAULT N'not_required' NULL,

        -- ── Primary key ──────────────────────────────────────
        CONSTRAINT PK_meetings
            PRIMARY KEY CLUSTERED (id ASC)
            WITH (FILLFACTOR = 90),          -- 10% free space for row growth

        -- ── Unique constraint on file path ───────────────────
        CONSTRAINT UQ_meetings_file_path
            UNIQUE NONCLUSTERED (file_path),

        -- ── Status domain check ──────────────────────────────
        CONSTRAINT CK_meetings_status CHECK (
            status IN (
                N'pending', N'processing', N'completed',
                N'failed', N'awaiting_approval', N'rejected'
            )
        ),

        -- ── Approval status domain check ─────────────────────
        CONSTRAINT CK_meetings_approval_status CHECK (
            approval_status IS NULL
            OR approval_status IN (
                N'not_required', N'pending', N'approved', N'rejected',
                N'auto_approved', N'auto_rejected'
            )
        )
    );

    PRINT 'Table dbo.meetings created.';
END
ELSE
BEGIN
    PRINT 'Table dbo.meetings already exists — skipped.';
END
GO


-- ────────────────────────────────────────────────────────────
-- Step 3: Create supporting indexes
-- ────────────────────────────────────────────────────────────

-- Index: fast status-based lookups (watcher checks if already processing)
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_meetings_status'
      AND object_id = OBJECT_ID(N'dbo.meetings')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_meetings_status
        ON dbo.meetings (status)
        INCLUDE (file_path, file_name, processed_at)
        WITH (FILLFACTOR = 90, ONLINE = OFF);

    PRINT 'Index IX_meetings_status created.';
END
GO

-- Index: recent-meetings dashboard query (ORDER BY processed_at DESC)
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_meetings_processed_at'
      AND object_id = OBJECT_ID(N'dbo.meetings')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_meetings_processed_at
        ON dbo.meetings (processed_at DESC)
        INCLUDE (
            file_name, meeting_title, status,
            teams_notified, email_sent, error_message
        )
        WITH (FILLFACTOR = 80, ONLINE = OFF);

    PRINT 'Index IX_meetings_processed_at created.';
END
GO

-- Index: date-range query used by weekly digest (Feature 10)
-- Filtered to completed meetings only — smaller, faster index
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_meetings_meeting_date_status'
      AND object_id = OBJECT_ID(N'dbo.meetings')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_meetings_meeting_date_status
        ON dbo.meetings (meeting_date ASC)
        INCLUDE (file_name, meeting_title, mom_json, attendance_json)
        WHERE status = N'completed'
        WITH (FILLFACTOR = 90, ONLINE = OFF);

    PRINT 'Index IX_meetings_meeting_date_status created.';
END
GO

-- Index: approval token lookup — sparse (only rows with a non-NULL token)
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_meetings_approval_token'
      AND object_id = OBJECT_ID(N'dbo.meetings')
)
BEGIN
    CREATE NONCLUSTERED INDEX IX_meetings_approval_token
        ON dbo.meetings (approval_token)
        WHERE approval_token IS NOT NULL
        WITH (FILLFACTOR = 100, ONLINE = OFF);

    PRINT 'Index IX_meetings_approval_token created.';
END
GO


-- ────────────────────────────────────────────────────────────
-- Step 4: Verify
-- ────────────────────────────────────────────────────────────

SELECT
    t.name          AS table_name,
    c.name          AS column_name,
    tp.name         AS data_type,
    c.max_length,
    c.is_nullable,
    c.is_identity,
    dc.definition   AS default_value
FROM sys.tables t
JOIN sys.columns c     ON c.object_id  = t.object_id
JOIN sys.types tp      ON tp.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints dc
    ON dc.parent_object_id = c.object_id
   AND dc.parent_column_id = c.column_id
WHERE t.name = N'meetings'
ORDER BY c.column_id;
GO

SELECT
    i.name          AS index_name,
    i.type_desc,
    i.is_unique,
    i.filter_definition
FROM sys.indexes i
WHERE i.object_id = OBJECT_ID(N'dbo.meetings')
  AND i.type > 0     -- exclude heap entry
ORDER BY i.index_id;
GO

PRINT '============================================================';
PRINT 'Schema setup complete. Database: MeetingSystem';
PRINT 'Server: ICS-LT-H3J9R73\SQLEXPRESS';
PRINT '============================================================';
GO
