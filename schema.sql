-- Run this in the Supabase SQL editor before deploying the app.
-- Creates the 3 tables used to persist runs, and locks them down with RLS
-- so only the service_role key (used server-side by the Streamlit app,
-- never exposed to the browser) can read/write.

create extension if not exists "pgcrypto";

create table if not exists runs (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    email_count int not null,
    model text
);

create table if not exists email_results (
    id uuid primary key default gen_random_uuid(),
    run_id uuid not null references runs(id) on delete cascade,
    created_at timestamptz not null default now(),
    email text not null,
    found boolean not null default false,
    good_to_collect boolean not null default true,
    flags text[] not null default '{}',
    error text
);

create table if not exists source_results (
    id uuid primary key default gen_random_uuid(),
    email_result_id uuid not null references email_results(id) on delete cascade,
    created_at timestamptz not null default now(),
    url text not null,
    title text,
    fetch_ok boolean not null default false,
    email_confirmed_on_page boolean not null default false,
    good_to_collect boolean not null default true,
    flags text[] not null default '{}',
    reasoning text,
    error text,
    dom_storage_path text
);

create index if not exists idx_email_results_run_id on email_results(run_id);
create index if not exists idx_source_results_email_result_id on source_results(email_result_id);

alter table runs enable row level security;
alter table email_results enable row level security;
alter table source_results enable row level security;

-- No policies are added for anon/authenticated roles — the app talks to
-- Postgres using the service_role key only, which bypasses RLS. This
-- keeps the data inaccessible via the public anon key.

-- Storage: create a PRIVATE bucket named 'dom-snapshots' (Storage -> New
-- bucket -> uncheck "Public bucket"). The app uploads to it and generates
-- short-lived signed URLs for download links, using the service_role key —
-- no storage policies are required for that path either.
