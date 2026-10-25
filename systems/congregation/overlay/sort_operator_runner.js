const fs = require('fs');
const path = require('path');
const http = require('http');

function usage() {
  console.error('usage: node sort_operator_runner.js CONFIG_JSON PARTY_DATA_CSV KEY_COL');
  process.exit(2);
}

if (process.argv.length !== 5 && process.argv.length !== 6) {
  usage();
}

const configPath = process.argv[2];
const dataPath = process.argv[3];
const keyCol = Number(process.argv[4]);
const expectedInputPath = process.argv[5] || '';
const cfg = JSON.parse(fs.readFileSync(configPath, 'utf8'));

const pid = Number(cfg.general.pid);
const allParties = cfg.general.all_pids.map(Number);
const partyCount = allParties.length;
const jiffLib = cfg.jiff.jiff_lib_path;
const serverIp = cfg.jiff.server_ip;
const serverPort = Number(cfg.jiff.server_port);
const serverPid = Number(cfg.jiff.server_pid);
const zp = Number(cfg.jiff.zp || 16777729);
const computationId = 'fes-bench-congregation-sort';

const methods = require('./methods');
const JIFFClient = require(path.join(jiffLib, 'lib/jiff-client.js'));

function readCsv(filePath) {
  const text = fs.readFileSync(filePath, 'utf8').trim();
  if (text.length === 0) {
    throw new Error(`empty party data file: ${filePath}`);
  }
  const lines = text.split(/\r?\n/);
  const rows = [];
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim().length === 0) {
      continue;
    }
    rows.push(lines[i].split(',').map((v) => Number(v)));
  }
  return rows;
}

function sharePlaintext(jiffInstance, inputParty) {
  const isOwner = pid === inputParty;
  const parsedData = isOwner ? readCsv(dataPath) : null;
  const keepRows = isOwner ? parsedData.map(() => 1) : null;
  return [
    jiffInstance.share_ND_array(parsedData, null, null, allParties, [inputParty]),
    jiffInstance.share_array(keepRows, null, null, allParties, [inputParty])
  ];
}

async function computeSort(jiffInstance) {
  const rels = [];
  const keepRows = [];
  for (const inputParty of allParties) {
    const [relPromise, keepRowsPromise] = sharePlaintext(jiffInstance, inputParty);
    rels.push(relPromise);
    keepRows.push(keepRowsPromise);
  }

  const [resolvedRels, resolvedKeepRows] = await Promise.all([
    Promise.all(rels),
    Promise.all(keepRows)
  ]);
  const sharedRels = [];
  const sharedKeepRows = [];
  for (let i = 0; i < allParties.length; i++) {
    const inputParty = allParties[i];
    sharedRels.push(resolvedRels[i][inputParty]);
    sharedKeepRows.push(resolvedKeepRows[i][inputParty]);
  }

  const concatenated = methods.concat(sharedRels, sharedKeepRows);
  const sortStartNs = process.hrtime.bigint();
  const sorted = await methods.oddEvenSort(concatenated[0], concatenated[1], keyCol, jiffInstance);
  const sortSeconds = Number(process.hrtime.bigint() - sortStartNs) / 1e9;
  // Reconstruction and complete-tuple validation follow the measured sort call.
  const opened = await methods.open(sorted[0], sorted[1], jiffInstance);
  if (expectedInputPath) {
    const expected = fs.readFileSync(expectedInputPath, 'utf8').trim().split(/\r?\n/).map(line => line.trim().split(/\s+/).map(Number));
    if (opened.length !== expected.length || opened.some((row, i) => i > 0 && opened[i - 1][keyCol] > row[keyCol])) {
      throw new Error('Sorting output has the wrong cardinality or order');
    }
    const rows = relation => relation.map(row => JSON.stringify(row.map(Number))).sort();
    if (JSON.stringify(rows(opened)) !== JSON.stringify(rows(expected))) throw new Error('Complete tuple multiset differs from original input');
    console.log(`SORT_TUPLES_VERIFIED rows=${opened.length} cols=${expected[0].length}`);
  }
  return sortSeconds;
}

function peakMemoryKb() {
  if (typeof process.resourceUsage === 'function') {
    return process.resourceUsage().maxRSS;
  }
  return Math.ceil(process.memoryUsage().rss / 1024);
}

let httpServer = null;

function startServerIfNeeded() {
  if (pid !== serverPid) {
    return Promise.resolve();
  }

  return new Promise((resolve, reject) => {
    const express = require('express');
    const app = express();
    httpServer = http.Server(app);
    const JIFFServer = require(path.join(jiffLib, 'lib/jiff-server'));
    new JIFFServer(httpServer, { logs: false });
    app.use('/demos', express.static(path.join(jiffLib, 'demos')));
    app.use('/dist', express.static(path.join(jiffLib, 'dist')));
    app.use('/lib/ext', express.static(path.join(jiffLib, 'lib/ext')));
    httpServer.on('error', reject);
    httpServer.listen(serverPort, '0.0.0.0', () => {
      console.log(`listening on *:${serverPort}`);
      resolve();
    });
  });
}

function finish(code) {
  if (httpServer != null) {
    httpServer.close(() => process.exit(code));
    setTimeout(() => process.exit(code), 1000);
  } else {
    process.exit(code);
  }
}

async function main() {
  await startServerIfNeeded();
  if (pid !== serverPid) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  const options = {
    party_count: partyCount,
    party_id: pid,
    Zp: zp,
    crypto_provider: true,
    warn: false,
    autoConnect: false
  };

  options.onConnect = function (jiffInstance) {
    const startNs = process.hrtime.bigint();
    computeSort(jiffInstance).then((sortElapsed) => {
      const elapsed = Number(process.hrtime.bigint() - startNs) / 1e9;
      console.log(`Sort-only computation time: ${sortElapsed.toFixed(6)} seconds`);
      console.log(`Total computation time: ${elapsed.toFixed(2)} seconds`);
      console.log(`Peak memory usage: ${peakMemoryKb()} KB`);
      jiffInstance.disconnect(false, true);
      setTimeout(() => finish(0), 250);
    }).catch((err) => {
      console.error(err && err.stack ? err.stack : String(err));
      try {
        jiffInstance.disconnect(false, true);
      } catch (e) {
        // Ignore disconnect errors while already failing the benchmark.
      }
      setTimeout(() => finish(1), 250);
    });
  };

  const jiffInstance = new JIFFClient(
    `http://${serverIp}:${serverPort}`,
    computationId,
    options
  );
  jiffInstance.connect();
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  finish(1);
});
