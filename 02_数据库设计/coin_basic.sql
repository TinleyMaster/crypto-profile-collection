/*
 Navicat Premium Data Transfer

 Source Server         : crypto
 Source Server Type    : PostgreSQL
 Source Server Version : 170010 (170010)
 Source Host           : 43.166.198.83:32405
 Source Catalog        : crypto
 Source Schema         : public

 Target Server Type    : PostgreSQL
 Target Server Version : 170010 (170010)
 File Encoding         : 65001

 Date: 24/07/2026 16:45:42
*/


-- ----------------------------
-- Table structure for coin_basic
-- ----------------------------
DROP TABLE IF EXISTS "public"."coin_basic";
CREATE TABLE "public"."coin_basic" (
  "coin_symbol" varchar(32) COLLATE "pg_catalog"."default" NOT NULL,
  "coin_name" varchar(128) COLLATE "pg_catalog"."default",
  "defillama_slug" varchar(128) COLLATE "pg_catalog"."default",
  "coingecko_id" varchar(128) COLLATE "pg_catalog"."default",
  "cmc_id" int8 NOT NULL,
  "category" varchar(64) COLLATE "pg_catalog"."default",
  "main_chain" varchar(64) COLLATE "pg_catalog"."default",
  "contract_addresses" jsonb,
  "total_supply" numeric,
  "team_allocation" numeric,
  "investor_allocation" numeric,
  "community_allocation" numeric,
  "audit_status" varchar(32) COLLATE "pg_catalog"."default",
  "audit_firm" varchar(128) COLLATE "pg_catalog"."default",
  "audit_report_url" text COLLATE "pg_catalog"."default",
  "official_website" text COLLATE "pg_catalog"."default",
  "docs_url" text COLLATE "pg_catalog"."default",
  "github_url" text COLLATE "pg_catalog"."default",
  "financing_amount" numeric,
  "investors" text COLLATE "pg_catalog"."default",
  "launch_date" date,
  "track_status" varchar(32) COLLATE "pg_catalog"."default" DEFAULT '未跟踪'::character varying,
  "last_updated" timestamp(6),
  "remark" text COLLATE "pg_catalog"."default",
  "cmc_slug" text COLLATE "pg_catalog"."default"
)
;

-- ----------------------------
-- Uniques structure for table coin_basic
-- ----------------------------
ALTER TABLE "public"."coin_basic" ADD CONSTRAINT "coin_basic_defillama_slug_key" UNIQUE ("defillama_slug");
ALTER TABLE "public"."coin_basic" ADD CONSTRAINT "coin_basic_cmc_id_key" UNIQUE ("cmc_id");

-- ----------------------------
-- Primary Key structure for table coin_basic
-- ----------------------------
ALTER TABLE "public"."coin_basic" ADD CONSTRAINT "coin_basic_pkey" PRIMARY KEY ("cmc_id");
