import type { CustomFieldType, CustomizableEntity, Role } from "@atlas/contracts";
import { sql } from "drizzle-orm";
import {
  bigserial,
  boolean,
  index,
  integer,
  jsonb,
  pgTable,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";
import { authUsers } from "drizzle-orm/supabase";

/*
 * Every table has Row-Level Security switched on with no policies, so the Supabase anon/authenticated keys
 * can't read or write anything directly. All access goes through the API, which connects as the database owner
 * and enforces the business rules. Add policies only if a client must ever talk to a table directly.
 */

const createdAt = timestamp("created_at", { withTimezone: true, mode: "string" }).notNull().defaultNow();
const updatedAt = timestamp("updated_at", { withTimezone: true, mode: "string" }).notNull().defaultNow();

/** Atlas profile for a Supabase Auth user: their role and branch. Supabase owns the login; Atlas owns permissions. */
export const appUsers = pgTable("app_users", {
  id: uuid("id")
    .primaryKey()
    .references(() => authUsers.id, { onDelete: "cascade" }),
  displayName: text("display_name").notNull(),
  role: text("role").$type<Role>().notNull().default("viewer"),
  branchId: uuid("branch_id"),
  isApprover: boolean("is_approver").notNull().default(false),
  active: boolean("active").notNull().default(true),
  createdAt,
  updatedAt,
}).enableRLS();

/**
 * User-defined fields. Values live in each entity's `custom` column, keyed by `key`.
 * A field is retired by setting `active = false`, never deleted, so old records stay readable.
 */
export const fieldDefinitions = pgTable(
  "field_definitions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    entity: text("entity").$type<CustomizableEntity>().notNull(),
    key: text("key").notNull(),
    label: text("label").notNull(),
    type: text("type").$type<CustomFieldType>().notNull(),
    required: boolean("required").notNull().default(false),
    helpText: text("help_text"),
    options: jsonb("options").$type<string[]>(),
    sortOrder: integer("sort_order").notNull().default(0),
    active: boolean("active").notNull().default(true),
    createdBy: uuid("created_by").references(() => appUsers.id),
    createdAt,
    updatedAt,
  },
  (t) => [uniqueIndex("field_definitions_entity_key").on(t.entity, t.key)],
).enableRLS();

export const suppliers = pgTable(
  "suppliers",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    name: text("name").notNull(),
    tin: text("tin"),
    address: text("address"),
    terms: text("terms"),
    custom: jsonb("custom").$type<Record<string, unknown>>().notNull().default(sql`'{}'::jsonb`),
    createdAt,
    updatedAt,
  },
  (t) => [
    uniqueIndex("suppliers_name_lower").on(sql`lower(${t.name})`),
    index("suppliers_custom_gin").using("gin", t.custom),
  ],
).enableRLS();

/** Append-only record of who did what. The desktop app blocks UPDATE/DELETE with triggers; port those with the first real migration. */
export const auditLogs = pgTable(
  "audit_logs",
  {
    id: bigserial("id", { mode: "number" }).primaryKey(),
    at: timestamp("at", { withTimezone: true, mode: "string" }).notNull().defaultNow(),
    userId: uuid("user_id").references(() => appUsers.id),
    action: text("action").notNull(),
    entity: text("entity").notNull(),
    entityId: text("entity_id"),
    details: jsonb("details").$type<Record<string, unknown>>(),
  },
  (t) => [index("audit_logs_entity").on(t.entity, t.entityId)],
).enableRLS();
