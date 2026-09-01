#!/usr/bin/env node
/**
 * eve-jita-poller — Cloud Run Job.
 *
 * Scans ESI market data and writes results to BigQuery. Meant to run on a Cloud Scheduler
 * trigger, but `node poller.js` also works standalone from any machine with:
 *   - Application Default Credentials set up (`gcloud auth application-default login`), or
 *   - running inside GCP with a service account attached (Cloud Run Job's normal mode)
 * and the env vars below set.
 *
 * Item universe (ITEM_MODE):
 *   "top_volume" (default) — fetches every type_id currently trading in the region
 *     (/markets/{region}/types/), ranks all of them by average daily trade volume over the
 *     last HISTORY_DAYS days (one ESI history call per type_id — this is the expensive pass —
 *     and it touches the WHOLE region regardless of TOP_N_ITEMS, since ranking has to happen
 *     before the cutoff), and keeps the top TOP_N_ITEMS. This is what actually runs on the
 *     schedule now: broad, real-liquidity-based coverage instead of a hand-picked list.
 *
 *     On top of the TOP_N_ITEMS cutoff, this mode also force-includes every type_id currently
 *     held in Matej's own open orders (`my_orders`, deduped/filtered the same way eval/orders.py
 *     does) — added 2026-09-01, see FORCE_INCLUDE_HELD_ORDERS below. Confirmed with Matej: a
 *     ranking cutoff, however wide, can never fully guarantee that something he's actually
 *     trading right now stays in it every single day, so this closes that gap unconditionally
 *     instead of relying on TOP_N_ITEMS width alone.
 *   "watchlist" — falls back to the original hand-picked WATCHLIST (+ CANDIDATES_PRICIER if
 *     SCAN_ALL=true). Kept as an escape hatch: cheap, fast, useful if the region types/ranking
 *     pass ever misbehaves or you just want the old fixed 52/84-item behavior back.
 *
 * Env vars:
 *   GCP_PROJECT_ID          (required) — BigQuery project to write into
 *   BQ_DATASET              (default: eve_jita_scanner)
 *   ITEM_MODE               (default: "top_volume") — "top_volume" | "watchlist"
 *   TOP_N_ITEMS             (default: 4000, was 750) — only used when ITEM_MODE=top_volume.
 *                            Raised 2026-09-01 per Matej — the ranking pass already walks the
 *                            whole region regardless of this number, so widening it only grows
 *                            the (cheaper) order-book-fetch phase, not the expensive ranking
 *                            pass. Trade-off: more market_orders_raw rows/day and a longer scan
 *                            (SCAN_CONCURRENCY-bound) — watch Cloud Run job duration/cost and
 *                            BigQuery storage growth after this change; task-timeout (3600s) and
 *                            memory (1Gi, deploy.sh) may need another bump if 4000 turns out to
 *                            still be too small or too slow.
 *   FORCE_INCLUDE_HELD_ORDERS (default: "true") — only used when ITEM_MODE=top_volume; set
 *                            "false" to disable the my_orders force-include described above.
 *   SCAN_ALL                (default: "false") — only used when ITEM_MODE=watchlist; set "true"
 *                            to also scan the 32 pricier-ships/minerals candidates
 *   HISTORY_DAYS            (default: 14) — turnover lookback window (also the ranking window
 *                            for top_volume mode)
 *   WRITE_RAW_ORDERS        (default: "true") — set "false" to skip the (larger) raw-orders table
 *   RANK_CONCURRENCY        (default: 10) — concurrent ESI requests during the history-ranking
 *                            pass (top_volume mode only — this pass touches every tradeable
 *                            type_id in the region, easily several thousand, so it needs to be
 *                            concurrent or the job runs for ages)
 *   SCAN_CONCURRENCY        (default: 6) — concurrent ESI requests while fetching full order
 *                            books for the kept items (heavier per-request than history, so a
 *                            lower concurrency than the ranking pass)
 *
 * A top_volume run touches thousands of type_ids just to rank them, so it takes a lot longer
 * than the old fixed-watchlist run (minutes, not seconds) — make sure the Cloud Run Job's
 * --task-timeout is generous (deploy.sh sets 3600s / 1h) and --memory is bumped (deploy.sh sets
 * 1Gi) to hold the larger in-memory row arrays before the BigQuery write.
 */

const { BigQuery } = require('@google-cloud/bigquery');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REGION_ID = 10000002; // The Forge
const JITA_STATION_ID = 60003760;
const ESI_BASE = 'https://esi.evetech.net/latest';
const BROKER = 0.01382;
const TAX = 0.03375;
const MAX_PAGES = 50; // safety cap on paginated order-book fetches for a single item
const MAX_TYPE_PAGES = 50; // safety cap on paginated /markets/{region}/types/ fetch

const PROJECT_ID = process.env.GCP_PROJECT_ID;
const DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
const ITEM_MODE = (process.env.ITEM_MODE || 'top_volume').toLowerCase();
const TOP_N_ITEMS = Number(process.env.TOP_N_ITEMS) || 4000;
const FORCE_INCLUDE_HELD_ORDERS = (process.env.FORCE_INCLUDE_HELD_ORDERS || 'true').toLowerCase() !== 'false';
const SCAN_ALL = (process.env.SCAN_ALL || 'false').toLowerCase() === 'true';
const HISTORY_DAYS = Number(process.env.HISTORY_DAYS) || 14;
const WRITE_RAW_ORDERS = (process.env.WRITE_RAW_ORDERS || 'true').toLowerCase() !== 'false';
const RANK_CONCURRENCY = Number(process.env.RANK_CONCURRENCY) || 10;
const SCAN_CONCURRENCY = Number(process.env.SCAN_CONCURRENCY) || 6;

if (!PROJECT_ID) {
  console.error('GCP_PROJECT_ID env var is required.');
  process.exit(1);
}

// ---- Fallback watchlist for ITEM_MODE=watchlist (kept in sync with esi-scan.js / SKILL.md) ----
const WATCHLIST = {
  'Medium Shield Booster II': 10850, 'Damage Control II': 2048, 'Gyrostabilizer II': 519,
  '1MN Afterburner II': 438, '10MN Afterburner II': 12058, 'Warp Scrambler II': 448,
  'Large Skill Injector': 40520, 'Small Skill Injector': 45635, 'Nanite Repair Paste': 28668,
  'Antimatter Charge L': 238, 'Mobile Tractor Unit': 33475, 'Sensor Booster II': 1952,
  'X5 Enduring Stasis Webifier': 4025, 'Arbalest Compact Light Missile Launcher': 8089,
  '200mm AutoCannon II': 2889, 'Small Armor Repairer II': 1183,
  'Medium Ancillary Current Router I': 31360, 'Co-Processor II': 3888,
  'Republic Fleet EMP L': 21894, 'Caldari Navy Antimatter Charge M': 23025,
  'Expanded Cargohold II': 1319, 'Ballistic Control System II': 22291,
  'Magnetic Field Stabilizer II': 10190, 'Drone Damage Amplifier II': 4405,
  '1600mm Steel Plates II': 20353, 'Warp Disruptor II': 3244, 'Tracking Computer II': 1978,
  'Medium Armor Repairer II': 3530, 'Heat Sink II': 2364, 'Signal Amplifier II': 1987,
  'Small Shield Booster II': 400, 'Large Shield Booster II': 10858,
  '425mm AutoCannon II': 2913, 'Heavy Missile Launcher II': 2410, 'Rocket Launcher II': 10631,
  'Remote Sensor Dampener II': 1969, 'Tracking Disruptor II': 2109,
  'Medium Trimark Armor Pump I': 31055, 'Medium Core Defense Field Extender I': 31790,
  'Hobgoblin II': 2456, 'Hammerhead II': 2185, 'Warrior II': 2488, 'Vespa EC-600': 23705,
  'Rifter': 587, 'Merlin': 603, 'Punisher': 597, 'Rupture': 629, 'Caracal': 621,
  'Vexor': 626, 'Catalyst': 16240, 'Thrasher': 16242, 'Cormorant': 16238,
};

const CANDIDATES_PRICIER = {
  'Myrmidon': 24700, 'Tempest Fleet Issue': 17732, 'Raven Navy Issue': 17636, 'Rokh': 24688,
  'Megathron Navy Issue': 17728, 'Golem': 28710, 'Hurricane': 24702, 'Harbinger': 24696,
  'Tengu': 29984, 'Armageddon': 643, 'Apocalypse': 642, 'Vexor Navy Issue': 17843,
  'Kronos': 28661, 'Abaddon': 24692, 'Tempest': 639, 'Proteus': 29988, 'Vargur': 28665,
  'Typhoon': 644, 'Drake': 24698, 'Paladin': 28659, 'Legion': 29986, 'Maelstrom': 24694,
  'Megathron': 641, 'Loki': 29990, 'Tritanium': 34, 'Pyerite': 35, 'Mexallon': 36,
  'Isogen': 37, 'Nocxium': 38, 'Zydrine': 39, 'Megacyte': 40, 'Morphite': 11399,
};

// Added 2026-08-28 — Matej wants ongoing coverage of two categories that fall through
// the cracks of both top_volume (too low-volume individually to crack top 750) and the
// hand-picked WATCHLIST (nobody's going to hand-curate every meta variant of every gun
// upgrade): Salvaged Materials, and the standard fitted T1/meta ship modules (armor
// plates/hardeners, shield extenders/boosters/hardeners, damage controls, weapon
// upgrades, tackle/EWAR, sensor/targeting mods, capacitor mods, propulsion). Resolved
// from ESI's live /markets/groups/ tree (not hand-typed type_ids) so it stays correct as
// CCP adds new items — see esi-jobs/../discover_market_groups.sh for how these group_ids
// were found. Deliberately excludes: blueprints, rigs, ammo/charges, implants/boosters,
// mutaplasmids, drones, and Upwell/POS structure modules — those are different trading
// questions Matej hasn't asked for yet.
const SALVAGE_GROUP_IDS = [
  1863, // Salvaged Materials
  1862, // Ancient Salvaged Materials
];
const MODULE_GROUP_IDS = [
  // Armor plates (by size) + hardeners (by damage type) + resistance coatings
  1672, 1673, 1674, 1675, 1676, 2240, 1678, 1679, 1680, 1681, 1416, 2245, 1669, 1687,
  // Shield extenders/boosters/hardeners (by damage type)/amplifiers/recharge & flux/remote
  1549, 1548, 1692, 1693, 1694, 1695, 1696, 2246, 1552, 1554, 1689, 1688, 1690, 1691,
  1551, 1550, 1547, 1553,
  // Damage controls, weapon upgrades (turret/missile)
  615, 645, 646, 648, 707, 706, 708, 2032, 2033,
  // Tackle / EWAR
  680, 683, 679, 1935, 1936, 1085,
  // Sensor / targeting
  669, 671, 673, 1583,
  // Capacitor
  659, 660, 1563, 1565, 1564,
  // Propulsion
  131, 542, 1086, 1088,
  // Misc fitted consumable
  1103,
];

// Resolves a list of ESI market_group_ids to the flat set of type_ids they contain
// (leaf groups only carry types directly; the tree's non-leaf "parent" groups have an
// empty types array and are simply not in this list). One ESI call per group_id —
// there's no bulk endpoint — so this is run with modest concurrency, same pattern as
// everything else in this file.
async function fetchMarketGroupTypeIds(groupIds) {
  const allTypeIds = new Set();
  await mapWithConcurrency(groupIds, 10, async (groupId) => {
    const { data } = await esiGet(`${ESI_BASE}/markets/groups/${groupId}/?datasource=tranquility`);
    for (const typeId of data.types || []) allTypeIds.add(typeId);
  });
  return [...allTypeIds];
}

async function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function esiGet(url, attempt = 1) {
  const res = await fetch(url, { headers: { Accept: 'application/json', 'User-Agent': 'eve-jita-poller/1.0 (Cloud Run Job)' } });
  const remain = res.headers.get('x-esi-error-limit-remain');
  if (remain !== null && Number(remain) < 10) await sleep(2000);
  if (res.status === 420 || res.status === 429) {
    if (attempt > 3) throw new Error(`Rate limited repeatedly on ${url}`);
    await sleep(3000 * attempt);
    return esiGet(url, attempt + 1);
  }
  if (!res.ok) throw new Error(`ESI ${res.status} on ${url}`);
  return { data: await res.json(), pages: Number(res.headers.get('x-pages')) || 1 };
}

// Runs fn(item, index) over items with at most `limit` in flight at once. Plain array-based
// worker pool — no external deps, good enough for a few thousand items.
async function mapWithConcurrency(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next++;
      try {
        results[i] = await fn(items[i], i);
      } catch (err) {
        results[i] = { __error: err };
      }
    }
  }
  const workers = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, worker);
  await Promise.all(workers);
  return results;
}

async function fetchAllRegionTypeIds() {
  const first = await esiGet(`${ESI_BASE}/markets/${REGION_ID}/types/?datasource=tranquility`);
  let ids = first.data;
  const pages = Math.min(first.pages, MAX_TYPE_PAGES);
  for (let p = 2; p <= pages; p++) {
    await sleep(100);
    const next = await esiGet(`${ESI_BASE}/markets/${REGION_ID}/types/?datasource=tranquility&page=${p}`);
    ids = ids.concat(next.data);
  }
  return ids;
}

// ESI's /universe/names/ takes up to 1000 ids per POST and returns {id, name, category} — this
// is the cheap way to get names for a few hundred/thousand type_ids without one call each.
async function fetchNames(typeIds) {
  const names = {};
  const CHUNK = 1000;
  for (let i = 0; i < typeIds.length; i += CHUNK) {
    const chunk = typeIds.slice(i, i + CHUNK);
    const res = await fetch(`${ESI_BASE}/universe/names/?datasource=tranquility`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'User-Agent': 'eve-jita-poller/1.0 (Cloud Run Job)' },
      body: JSON.stringify(chunk),
    });
    if (!res.ok) throw new Error(`ESI ${res.status} on /universe/names/`);
    const data = await res.json();
    for (const item of data) {
      if (item.category === 'inventory_type') names[item.id] = item.name;
    }
    if (i + CHUNK < typeIds.length) await sleep(100);
  }
  return names;
}

async function fetchAllOrders(typeId) {
  const first = await esiGet(`${ESI_BASE}/markets/${REGION_ID}/orders/?datasource=tranquility&order_type=all&type_id=${typeId}`);
  let orders = first.data;
  const pagesToFetch = Math.min(first.pages, MAX_PAGES);
  for (let p = 2; p <= pagesToFetch; p++) {
    await sleep(120);
    const next = await esiGet(`${ESI_BASE}/markets/${REGION_ID}/orders/?datasource=tranquility&order_type=all&page=${p}&type_id=${typeId}`);
    orders = orders.concat(next.data);
  }
  return orders;
  // Public region-orders endpoint — NPC stations only, no Perimeter citadel visibility. See
  // the local esi-scan.js README for why, and the tradeoff.
}

async function fetchHistory(typeId) {
  const { data } = await esiGet(`${ESI_BASE}/markets/${REGION_ID}/history/?datasource=tranquility&type_id=${typeId}`);
  return data;
}

// Ranks every type_id in `typeIds` by avg daily volume over the last `historyDays` days.
// Deliberately does NOT keep the full history payload per item — with 15-20k type_ids in the
// region, holding a year of daily records for every single one in memory at once is what OOM'd
// the container the first time around. Just keep the one aggregate number needed for sorting;
// the scan pass below re-fetches full history for the (much smaller) kept set.
async function rankByVolume(typeIds, historyDays, concurrency) {
  let done = 0;
  const results = await mapWithConcurrency(typeIds, concurrency, async (typeId) => {
    const history = await fetchHistory(typeId);
    const recent = history.slice(-historyDays);
    const avgVol = recent.length ? recent.reduce((s, d) => s + d.volume, 0) / recent.length : 0;
    done++;
    if (done % 1000 === 0) console.log(`  ranking progress: ${done}/${typeIds.length}`);
    return { typeId, avgVol };
  });
  const ranked = results.filter(r => r && !r.__error);
  ranked.sort((a, b) => b.avgVol - a.avgVol);
  return ranked;
}

// Historically this weight-averaged price across the top 5% of order-book *volume* on each
// side, meant to be more robust than a bare top-of-book price. In practice it backfired badly:
// EVE's raw-material/moon-good markets (Promethium, Strontium Clathrates, compressed gas/ore,
// ...) regularly carry a handful of enormous standing "floor" buy orders from industrial buyers
// — bulk bids sitting far below the real market, sometimes 10-50x a day's actual traded volume
// in size. Once even one such order falls inside the 5%-of-volume window, it swamps the weighted
// average and drags the reported buy price (and therefore the margin) way down from what you'd
// actually pay to be competitive. Confirmed against real numbers 2026-08-25: Promethium showed
// 28.6k here vs. an actual best buy order of 51.0k in-game; Strontium Clathrates showed 2.1k vs.
// an actual 3.95k. Both had a handful of orders holding order-of-magnitude more volume than a
// full day's trade — the floor-order pattern, not noise.
//
// Fix: just take the top-of-book price — the single best standing order on each side. That's
// exactly the number the game client shows you under "Market Buy/Sell", it's what you actually
// have to beat to be competitive, and it can't be dragged around by a giant order sitting deeper
// in the book. The one tradeoff is more sensitivity to a single one-off/troll order at the very
// top — acceptable, since that's rare and easy to eyeball, versus the floor-order problem, which
// was systemic across this item category.
function topOfBook(orders, side) {
  if (!orders.length) return null;
  const best = orders.reduce((b, o) => {
    if (!b) return o;
    return side === 'buy' ? (o.price > b.price ? o : b) : (o.price < b.price ? o : b);
  }, null);
  return best ? best.price : null;
}

function summarize(orders, stationFilter) {
  const filtered = stationFilter ? orders.filter(o => o.location_id === stationFilter) : orders;
  const buys = filtered.filter(o => o.is_buy_order);
  const sells = filtered.filter(o => !o.is_buy_order);
  return {
    buyOrders: buys.length,
    sellOrders: sells.length,
    buyVolume: buys.reduce((s, o) => s + o.volume_remain, 0),
    sellVolume: sells.reduce((s, o) => s + o.volume_remain, 0),
    buyAvg5: topOfBook(buys, 'buy'),
    sellAvg5: topOfBook(sells, 'sell'),
  };
}

function marginPct(buyAvg5, sellAvg5) {
  if (!buyAvg5 || !sellAvg5) return null;
  const buyCost = buyAvg5 * (1 + BROKER);
  const sellNet = sellAvg5 * (1 - BROKER - TAX);
  return (sellNet - buyCost) / buyCost * 100;
}

// Type_ids currently held in Matej's own open orders — force-included into top_volume mode
// regardless of volume rank (see FORCE_INCLUDE_HELD_ORDERS in the header comment). Same
// append-only-table dedupe (latest row per order_id) and is_open handling as eval/orders.py's
// OPEN_ORDERS_SQL: NULL is_open means a legacy pre-fix row, treated as still open.
async function fetchHeldTypeIds(bq) {
  const sql = `
    select distinct type_id, item_name
    from (
      select type_id, item_name, order_id, is_open,
             row_number() over (partition by order_id order by scanned_at desc) as rn
      from \`${PROJECT_ID}.${DATASET}.my_orders\`
    )
    where rn = 1 and (is_open is null or is_open = true)
  `;
  const [rows] = await bq.query({ query: sql });
  return rows.map(r => ({ typeId: r.type_id, name: r.item_name }));
}

async function buildItemList(bq) {
  if (ITEM_MODE === 'watchlist') {
    const items = { ...WATCHLIST, ...(SCAN_ALL ? CANDIDATES_PRICIER : {}) };
    if (SCAN_ALL) {
      console.log('Resolving Salvage + T1/meta module market groups from ESI...');
      const extraTypeIds = await fetchMarketGroupTypeIds([...SALVAGE_GROUP_IDS, ...MODULE_GROUP_IDS]);
      console.log(`Found ${extraTypeIds.length} type_ids across ${SALVAGE_GROUP_IDS.length + MODULE_GROUP_IDS.length} market groups. Looking up names...`);
      const extraNames = await fetchNames(extraTypeIds);
      for (const typeId of extraTypeIds) {
        items[extraNames[typeId] || `type_${typeId}`] = typeId;
      }
    }
    return Object.entries(items).map(([name, typeId]) => ({ name, typeId }));
  }

  console.log(`Fetching full market type list for region ${REGION_ID}...`);
  const allTypeIds = await fetchAllRegionTypeIds();
  console.log(`Region has ${allTypeIds.length} types with active orders. Ranking by avg daily volume (last ${HISTORY_DAYS}d, concurrency ${RANK_CONCURRENCY})...`);
  const ranked = await rankByVolume(allTypeIds, HISTORY_DAYS, RANK_CONCURRENCY);
  const top = ranked.slice(0, TOP_N_ITEMS).map(r => r.typeId);

  const combined = new Map(top.map(typeId => [typeId, null])); // name filled in below
  if (FORCE_INCLUDE_HELD_ORDERS) {
    try {
      const held = await fetchHeldTypeIds(bq);
      let added = 0;
      for (const { typeId, name } of held) {
        if (!combined.has(typeId)) added++;
        combined.set(typeId, name || null);
      }
      console.log(`Force-including ${held.length} type_ids currently held in my_orders (${added} were outside the top ${TOP_N_ITEMS} by volume).`);
    } catch (err) {
      // Non-fatal: don't let a my_orders hiccup (e.g. table momentarily stale/empty) take down
      // the whole scan — top_volume coverage alone still runs.
      console.log(`WARNING: could not fetch held order type_ids for force-include, continuing without it: ${err.message}`);
    }
  }

  const allIds = [...combined.keys()];
  console.log(`Looking up names for ${allIds.length} items...`);
  const names = await fetchNames(allIds);
  return allIds.map(typeId => ({ name: combined.get(typeId) || names[typeId] || `type_${typeId}`, typeId }));
}

async function main() {
  const bq = new BigQuery({ projectId: PROJECT_ID });
  const scannedAt = new Date();
  const scanDate = scannedAt.toISOString().slice(0, 10);

  const itemList = await buildItemList(bq);
  console.log(`Scanning ${itemList.length} items (mode=${ITEM_MODE}, region ${REGION_ID}, concurrency ${SCAN_CONCURRENCY}) at ${scannedAt.toISOString()}`);

  const snapshotRows = [];
  const rawOrderRows = [];
  const historyRows = [];

  await mapWithConcurrency(itemList, SCAN_CONCURRENCY, async ({ name, typeId }, idx) => {
    const label = `[${idx + 1}/${itemList.length}] ${name}`;
    try {
      const [orders, history] = await Promise.all([fetchAllOrders(typeId), fetchHistory(typeId)]);
      const region = summarize(orders, null);
      const station = summarize(orders, JITA_STATION_ID);
      const recentHistory = history.slice(-HISTORY_DAYS);
      const avgDailyVolume = recentHistory.length
        ? recentHistory.reduce((s, d) => s + d.volume, 0) / recentHistory.length
        : null;

      snapshotRows.push({
        scanned_at: scannedAt.toISOString(),
        scan_date: scanDate,
        type_id: typeId,
        item_name: name,
        region_buy_orders: region.buyOrders,
        region_sell_orders: region.sellOrders,
        region_buy_volume: region.buyVolume,
        region_sell_volume: region.sellVolume,
        region_buy_avg5: region.buyAvg5,
        region_sell_avg5: region.sellAvg5,
        region_margin_pct: marginPct(region.buyAvg5, region.sellAvg5),
        station_buy_orders: station.buyOrders,
        station_sell_orders: station.sellOrders,
        station_buy_volume: station.buyVolume,
        station_sell_volume: station.sellVolume,
        station_buy_avg5: station.buyAvg5,
        station_sell_avg5: station.sellAvg5,
        station_margin_pct: marginPct(station.buyAvg5, station.sellAvg5),
        avg_daily_volume_14d: avgDailyVolume,
        error: null,
      });

      if (WRITE_RAW_ORDERS) {
        for (const o of orders) {
          rawOrderRows.push({
            scanned_at: scannedAt.toISOString(),
            scan_date: scanDate,
            type_id: typeId,
            item_name: name,
            order_id: o.order_id,
            is_buy_order: o.is_buy_order,
            price: o.price,
            volume_remain: o.volume_remain,
            volume_total: o.volume_total,
            location_id: o.location_id,
            system_id: o.system_id,
            min_volume: o.min_volume,
            duration: o.duration,
            issued: o.issued,
          });
        }
      }

      for (const h of history) {
        historyRows.push({
          type_id: typeId,
          item_name: name,
          history_date: h.date,
          average: h.average,
          highest: h.highest,
          lowest: h.lowest,
          order_count: h.order_count,
          volume: h.volume,
          fetched_at: scannedAt.toISOString(),
        });
      }

      const marginTxt = station.buyAvg5 && station.sellAvg5 ? marginPct(station.buyAvg5, station.sellAvg5).toFixed(1) + '%' : 'n/a';
      if ((idx + 1) % 50 === 0 || idx === itemList.length - 1) {
        console.log(`${label}... ok (station margin ${marginTxt}) [${idx + 1}/${itemList.length} done]`);
      }
    } catch (err) {
      console.log(`${label}... FAILED: ${err.message}`);
      snapshotRows.push({
        scanned_at: scannedAt.toISOString(), scan_date: scanDate,
        type_id: typeId, item_name: name, error: String(err.message),
      });
    }
  });

  console.log(`\nWriting ${snapshotRows.length} snapshot rows, ${rawOrderRows.length} raw order rows, ${historyRows.length} history rows to BigQuery...`);

  const dataset = bq.dataset(DATASET);
  await insertInBatches(dataset.table('market_snapshots'), snapshotRows);
  if (WRITE_RAW_ORDERS && rawOrderRows.length) {
    await insertInBatches(dataset.table('market_orders_raw'), rawOrderRows);
  }
  // market_history is a full-window replace each run (ESI always returns ~1yr per item), so
  // load-and-truncate rather than stream-append, to avoid unbounded duplicate growth.
  //
  // Table.load() needs a local file path (or a GCS File object) as its source — it can't take a
  // Buffer/string directly ("Source must be a File object"). So we write the NDJSON to a temp
  // file in /tmp (writable in the Cloud Run container) and point .load() at that.
  if (historyRows.length) {
    const tmpFile = path.join(os.tmpdir(), `eve-history-${Date.now()}.jsonl`);
    fs.writeFileSync(tmpFile, historyRows.map(r => JSON.stringify(r)).join('\n'));
    try {
      await dataset.table('market_history').load(tmpFile, {
        sourceFormat: 'NEWLINE_DELIMITED_JSON',
        writeDisposition: 'WRITE_TRUNCATE',
      });
    } finally {
      fs.unlinkSync(tmpFile);
    }
  }

  console.log('Done.');
}

async function insertInBatches(table, rows, batchSize = 5000) {
  for (let i = 0; i < rows.length; i += batchSize) {
    const batch = rows.slice(i, i + batchSize);
    try {
      await table.insert(batch, { skipInvalidRows: false, ignoreUnknownValues: false });
    } catch (err) {
      // BigQuery insert errors often carry per-row detail in err.errors — surface it, don't swallow it.
      console.error(`Insert failed for batch starting at row ${i}:`, JSON.stringify(err.errors || err.message).slice(0, 2000));
      throw err;
    }
  }
}

main().catch(err => {
  console.error('Poller run failed:', err);
  process.exit(1);
});
