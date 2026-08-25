#!/usr/bin/env node
/**
 * eve-jita-poller — Cloud Run Job.
 *
 * Scans ESI market data for the watchlist (same items/logic as the local esi-scan.js script)
 * and writes results to BigQuery instead of a local JSON file. Meant to run on a Cloud Scheduler
 * trigger, but `node poller.js` also works standalone from any machine with:
 *   - Application Default Credentials set up (`gcloud auth application-default login`), or
 *   - running inside GCP with a service account attached (Cloud Run Job's normal mode)
 * and the env vars below set.
 *
 * Env vars:
 *   GCP_PROJECT_ID          (required) — BigQuery project to write into
 *   BQ_DATASET              (default: eve_jita_scanner)
 *   SCAN_ALL                (default: "false") — set to "true" to also scan the 32
 *                            pricier-ships/minerals candidates
 *   HISTORY_DAYS            (default: 14) — turnover lookback window
 *   WRITE_RAW_ORDERS        (default: "true") — set "false" to skip the (larger) raw-orders table
 */

const { BigQuery } = require('@google-cloud/bigquery');

const REGION_ID = 10000002; // The Forge
const JITA_STATION_ID = 60003760;
const ESI_BASE = 'https://esi.evetech.net/latest';
const BROKER = 0.01382;
const TAX = 0.03375;
const MAX_PAGES = 50;

const PROJECT_ID = process.env.GCP_PROJECT_ID;
const DATASET = process.env.BQ_DATASET || 'eve_jita_scanner';
const SCAN_ALL = (process.env.SCAN_ALL || 'false').toLowerCase() === 'true';
const HISTORY_DAYS = Number(process.env.HISTORY_DAYS) || 14;
const WRITE_RAW_ORDERS = (process.env.WRITE_RAW_ORDERS || 'true').toLowerCase() !== 'false';

if (!PROJECT_ID) {
  console.error('GCP_PROJECT_ID env var is required.');
  process.exit(1);
}

// ---- Watchlist (kept in sync with esi-scan.js / SKILL.md — update both if you add items) ----
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

function weightedAvgTopFraction(orders, side, fraction = 0.05) {
  if (!orders.length) return null;
  const sorted = [...orders].sort((a, b) => side === 'buy' ? b.price - a.price : a.price - b.price);
  const totalVol = sorted.reduce((s, o) => s + o.volume_remain, 0);
  const targetVol = Math.max(totalVol * fraction, sorted[0].volume_remain);
  let acc = 0, weightedSum = 0;
  for (const o of sorted) {
    const take = Math.min(o.volume_remain, targetVol - acc);
    if (take <= 0) break;
    weightedSum += o.price * take;
    acc += take;
  }
  return acc > 0 ? weightedSum / acc : sorted[0].price;
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
    buyAvg5: weightedAvgTopFraction(buys, 'buy'),
    sellAvg5: weightedAvgTopFraction(sells, 'sell'),
  };
}

function marginPct(buyAvg5, sellAvg5) {
  if (!buyAvg5 || !sellAvg5) return null;
  const buyCost = buyAvg5 * (1 + BROKER);
  const sellNet = sellAvg5 * (1 - BROKER - TAX);
  return (sellNet - buyCost) / buyCost * 100;
}

async function scanItem(name, typeId) {
  const [orders, history] = await Promise.all([fetchAllOrders(typeId), fetchHistory(typeId)]);
  const region = summarize(orders, null);
  const station = summarize(orders, JITA_STATION_ID);
  const recentHistory = history.slice(-HISTORY_DAYS);
  const avgDailyVolume = recentHistory.length
    ? recentHistory.reduce((s, d) => s + d.volume, 0) / recentHistory.length
    : null;
  return { name, typeId, orders, history, region, station, avgDailyVolume };
}

async function main() {
  const bq = new BigQuery({ projectId: PROJECT_ID });
  const items = { ...WATCHLIST, ...(SCAN_ALL ? CANDIDATES_PRICIER : {}) };
  const names = Object.keys(items);
  const scannedAt = new Date();
  const scanDate = scannedAt.toISOString().slice(0, 10);

  console.log(`Scanning ${names.length} items (region ${REGION_ID}) at ${scannedAt.toISOString()}`);

  const snapshotRows = [];
  const rawOrderRows = [];
  const historyRows = [];

  let i = 0;
  for (const name of names) {
    i++;
    process.stdout.write(`[${i}/${names.length}] ${name}... `);
    try {
      const r = await scanItem(name, items[name]);
      snapshotRows.push({
        scanned_at: scannedAt.toISOString(),
        scan_date: scanDate,
        type_id: r.typeId,
        item_name: r.name,
        region_buy_orders: r.region.buyOrders,
        region_sell_orders: r.region.sellOrders,
        region_buy_volume: r.region.buyVolume,
        region_sell_volume: r.region.sellVolume,
        region_buy_avg5: r.region.buyAvg5,
        region_sell_avg5: r.region.sellAvg5,
        region_margin_pct: marginPct(r.region.buyAvg5, r.region.sellAvg5),
        station_buy_orders: r.station.buyOrders,
        station_sell_orders: r.station.sellOrders,
        station_buy_volume: r.station.buyVolume,
        station_sell_volume: r.station.sellVolume,
        station_buy_avg5: r.station.buyAvg5,
        station_sell_avg5: r.station.sellAvg5,
        station_margin_pct: marginPct(r.station.buyAvg5, r.station.sellAvg5),
        avg_daily_volume_14d: r.avgDailyVolume,
        error: null,
      });
      if (WRITE_RAW_ORDERS) {
        for (const o of r.orders) {
          rawOrderRows.push({
            scanned_at: scannedAt.toISOString(),
            scan_date: scanDate,
            type_id: r.typeId,
            item_name: r.name,
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
      for (const h of r.history) {
        historyRows.push({
          type_id: r.typeId,
          item_name: r.name,
          history_date: h.date,
          average: h.average,
          highest: h.highest,
          lowest: h.lowest,
          order_count: h.order_count,
          volume: h.volume,
          fetched_at: scannedAt.toISOString(),
        });
      }
      console.log(`ok (station margin ${r.station.buyAvg5 && r.station.sellAvg5 ? marginPct(r.station.buyAvg5, r.station.sellAvg5).toFixed(1) + '%' : 'n/a'})`);
    } catch (err) {
      console.log(`FAILED: ${err.message}`);
      snapshotRows.push({
        scanned_at: scannedAt.toISOString(), scan_date: scanDate,
        type_id: items[name], item_name: name, error: String(err.message),
      });
    }
    await sleep(150);
  }

  console.log(`\nWriting ${snapshotRows.length} snapshot rows, ${rawOrderRows.length} raw order rows, ${historyRows.length} history rows to BigQuery...`);

  const dataset = bq.dataset(DATASET);
  await insertInBatches(dataset.table('market_snapshots'), snapshotRows);
  if (WRITE_RAW_ORDERS && rawOrderRows.length) {
    await insertInBatches(dataset.table('market_orders_raw'), rawOrderRows);
  }
  // market_history is a full-window replace each run (ESI always returns ~1yr), so load-and-truncate
  // rather than stream-append, to avoid unbounded duplicate growth.
  if (historyRows.length) {
    await dataset.table('market_history').load(Buffer.from(historyRows.map(r => JSON.stringify(r)).join('\n')), {
      sourceFormat: 'NEWLINE_DELIMITED_JSON',
      writeDisposition: 'WRITE_TRUNCATE',
    });
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
