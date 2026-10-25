package edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort;

import edu.alibaba.mpc4j.common.rpc.MpcAbortException;
import edu.alibaba.mpc4j.common.rpc.Rpc;
import edu.alibaba.mpc4j.common.rpc.desc.SecurityModel;
import edu.alibaba.mpc4j.common.rpc.impl.netty.NettyParty;
import edu.alibaba.mpc4j.common.rpc.impl.netty.simple.SimpleNettyRpc;
import edu.alibaba.mpc4j.common.rpc.pto.AbstractThreePartyMemoryRpcPto;
import edu.alibaba.mpc4j.common.rpc.utils.DataPacket;
import edu.alibaba.mpc4j.common.rpc.utils.DataPacketHeader;
import edu.alibaba.mpc4j.common.structure.vector.LongVector;
import edu.alibaba.mpc4j.common.tool.utils.IntUtils;
import edu.alibaba.mpc4j.s3pc.abb3.basic.Abb3Party;
import edu.alibaba.mpc4j.s3pc.abb3.basic.Abb3RpConfig;
import edu.alibaba.mpc4j.s3pc.abb3.basic.Abb3RpParty;
import edu.alibaba.mpc4j.s3pc.abb3.structure.zlong.TripletLongVector;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteFactory;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteOperations.PermuteFnParam;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteOperations.PermuteOp;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteParty;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.PgSortOperations.PgSortFnParam;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.PgSortOperations.PgSortOp;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.bitonic.BitonicPgSortConfig;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.quick.QuickPgSortConfig;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.radix.RadixPgSortConfig;
import org.apache.commons.lang3.time.StopWatch;
import org.junit.Assert;
import org.junit.Test;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.IntStream;

public class PgSortLanParty {
    private static final Logger LOGGER = LoggerFactory.getLogger(PgSortLanParty.class);
    private static final boolean MATERIALIZE_FULL_TABLE = true;
    private static final long MEMORY_SAMPLE_INTERVAL_MILLIS = 5L;
    private static final SecureRandom SECURE_RANDOM = new SecureRandom();

    private final Rpc firstRpc = null;
    private final Rpc secondRpc = null;
    private final Rpc thirdRpc = null;
    private final RuntimeConfig runtimeConfig;
    private final PgSortConfig sortConfig;
    private final boolean baseUseMac;

    public PgSortLanParty() {
        runtimeConfig = RuntimeConfig.fromSystemProperties();
        sortConfig = createSortConfig(runtimeConfig);
        baseUseMac = runtimeConfig.baseUseMac;
    }

    @Test
    public void testSingleConfigFromFile() {
        FileInputData inputData = readInputData(runtimeConfig);

        LOGGER.info(
            "========== PgSort full-table test: input={}, algorithm={}, security={}, keyNum={}, keyBitLens={}, rows={}, cols={}, parallel={}, materializeFullTable={} ==========",
            runtimeConfig.inputPath,
            runtimeConfig.algorithm,
            runtimeConfig.security,
            runtimeConfig.keyNum,
            Arrays.toString(inputData.keyBitLens),
            inputData.rowNum,
            inputData.totalCols,
            runtimeConfig.parallel,
            MATERIALIZE_FULL_TABLE
        );

        testOpi(runtimeConfig.parallel, inputData);
    }

    public static void main(String[] args) throws Exception {
        RuntimeConfig config = RuntimeConfig.fromSystemProperties();
        PgSortConfig sortConfig = createSortConfig(config);
        int rank = Integer.parseInt(System.getProperty("mpc4j.lan.rank"));
        int taskId = Integer.parseInt(System.getProperty("mpc4j.lan.taskId", "70001"));
        String partiesSpec = System.getProperty("mpc4j.lan.parties");
        if (partiesSpec == null || partiesSpec.trim().isEmpty()) {
            throw new IllegalArgumentException("Missing -Dmpc4j.lan.parties=host0:port0,host1:port1,host2:port2");
        }

        Rpc rpc = createLanRpc(rank, partiesSpec);
        FileInputData inputData = readInputData(config);
        boolean isMalicious = sortConfig.getSecurityModel().equals(SecurityModel.MALICIOUS);
        Abb3RpConfig abb3RpConfig = new Abb3RpConfig.Builder(isMalicious, config.baseUseMac).build();

        Abb3Party abb3Party = new Abb3RpParty(rpc, abb3RpConfig);
        PgSortParty sortParty = PgSortFactory.createParty(abb3Party, sortConfig);
        PermuteParty permuteParty = PermuteFactory.createParty(
            abb3Party,
            PermuteFactory.createDefaultConfig(sortConfig.getSecurityModel())
        );
        sortParty.setParallel(config.parallel);
        sortParty.setTaskId(taskId);
        permuteParty.setParallel(config.parallel);
        permuteParty.setTaskId(taskId);

        rpc.connect();
        PeakMemoryMonitor memoryMonitor = new PeakMemoryMonitor(MEMORY_SAMPLE_INTERVAL_MILLIS);
        StopWatch stopWatch = new StopWatch();
        long sendBefore = rpc.getSendByteLength();
        FullTablePartyThread thread = new FullTablePartyThread(sortParty, permuteParty, inputData);
        thread.setUncaughtExceptionHandler((failedThread, error) -> thread.failure = error);
        try {
            memoryMonitor.start();
            stopWatch.start();
            thread.start();
            thread.join();
            if (thread.failure != null) {
                throw new RuntimeException("Sorting party failed", thread.failure);
            }
            stopWatch.stop();
            memoryMonitor.stop();
            if (rank == 0) {
                verifyFullTable(inputData, thread.getPai(), thread.getSortedTable());
            }
        } finally {
            try {
                sortParty.destroy();
            } catch (Exception error) {
                throw new RuntimeException("Native protocol cleanup failed", error);
            }
        }

        long timeMs = stopWatch.getTime(TimeUnit.MILLISECONDS);
        long sendBytes = Math.max(0L, rpc.getSendByteLength() - sendBefore);
        double commMB = sendBytes / (1024.0 * 1024.0);
        double sortOnlyTimeS = thread.getSortOnlyTimeMs() / 1000.0;
        double sortOnlyCommMB = thread.getSortOnlySendBytes() / (1024.0 * 1024.0);
        double peakHeapMB = memoryMonitor.getPeakUsedBytes() / (1024.0 * 1024.0);
        System.out.println(
            "RESULT_E2E_CSV,mpc4j_lan," + rank + "," + inputData.rowNum + "," + inputData.totalCols + ","
                + inputData.keyBitLens.length + "," + config.bitLen + "," + config.algorithm + ","
                + String.format(Locale.ROOT, "%.6f", timeMs / 1000.0) + ","
                + String.format(Locale.ROOT, "%.6f", commMB) + ","
                + String.format(Locale.ROOT, "%.6f", peakHeapMB)
        );
        System.out.println(
            "RESULT_CSV,mpc4j_lan," + rank + "," + inputData.rowNum + "," + inputData.totalCols + ","
                + inputData.keyBitLens.length + "," + config.bitLen + "," + config.algorithm + ","
                + String.format(Locale.ROOT, "%.6f", sortOnlyTimeS) + ","
                + String.format(Locale.ROOT, "%.6f", sortOnlyCommMB) + ","
                + String.format(Locale.ROOT, "%.6f", thread.sortOnlyPeakRssBytes / (1024.0 * 1024.0))
        );
        System.out.println("SORT_PARTY_COMPLETED,rank=" + rank + ",security=" + config.security
            + ",materials=REAL,checks=COMPLETED,full_tuple=" + (rank == 0 ? "VERIFIED" : "rank0"));
        rpc.disconnect();
    }

    private static Rpc createLanRpc(int rank, String partiesSpec) {
        String[] parts = partiesSpec.split(",");
        Set<NettyParty> parties = new HashSet<>();
        NettyParty ownParty = null;
        for (int i = 0; i < parts.length; i++) {
            String[] hostPort = parts[i].trim().split(":");
            if (hostPort.length != 2) {
                throw new IllegalArgumentException("Bad party endpoint: " + parts[i]);
            }
            NettyParty party = new NettyParty(i, "P_" + (i + 1), hostPort[0], Integer.parseInt(hostPort[1]));
            parties.add(party);
            if (i == rank) {
                ownParty = party;
            }
        }
        if (ownParty == null) {
            throw new IllegalArgumentException("mpc4j.lan.rank out of range");
        }
        return new SimpleNettyRpc(ownParty, parties);
    }

    private static PgSortConfig createSortConfig(RuntimeConfig runtimeConfig) {
        boolean malicious = runtimeConfig.security.equals("malicious");
        switch (runtimeConfig.algorithm) {
            case 1:
                return new BitonicPgSortConfig.Builder(malicious)
                    .setStableSign(runtimeConfig.stable)
                    .build();
            case 2:
                return new QuickPgSortConfig.Builder(malicious).build();
            case 3:
                return new RadixPgSortConfig.Builder(malicious).build();
            default:
                throw new IllegalArgumentException("pgsort.algorithm must be 1, 2, or 3");
        }
    }

    private ProtocolParties getParties(boolean parallel) {
        Rpc[] rpcAll = new Rpc[]{firstRpc, secondRpc, thirdRpc};
        boolean isMalicious = sortConfig.getSecurityModel().equals(SecurityModel.MALICIOUS);

        Abb3RpConfig abb3RpConfig = new Abb3RpConfig.Builder(isMalicious, baseUseMac).build();

        Abb3Party[] abb3Parties = IntStream.range(0, 3)
            .mapToObj(i -> new Abb3RpParty(rpcAll[i], abb3RpConfig))
            .toArray(Abb3RpParty[]::new);

        PgSortParty[] sortParties = Arrays.stream(abb3Parties)
            .map(each -> PgSortFactory.createParty(each, sortConfig))
            .toArray(PgSortParty[]::new);

        PermuteParty[] permuteParties = Arrays.stream(abb3Parties)
            .map(each -> PermuteFactory.createParty(each, PermuteFactory.createDefaultConfig(sortConfig.getSecurityModel())))
            .toArray(PermuteParty[]::new);

        int randomTaskId = Math.abs(SECURE_RANDOM.nextInt());
        for (int i = 0; i < sortParties.length; i++) {
            sortParties[i].setParallel(parallel);
            sortParties[i].setTaskId(randomTaskId);
            permuteParties[i].setParallel(parallel);
            permuteParties[i].setTaskId(randomTaskId);
        }
        return new ProtocolParties(sortParties, permuteParties);
    }

    private void testOpi(boolean parallel, FileInputData inputData) {
        ProtocolParties parties = getParties(parallel);
        PgSortParty[] sortParties = parties.sortParties;
        PermuteParty[] permuteParties = parties.permuteParties;

        try {
            LOGGER.info(
                "-----test {}, rows={}, cols={}, keyNum={}, keyBitLens={}, materializeFullTable:{} start-----",
                sortParties[0].getPtoDesc().getPtoName(),
                inputData.rowNum,
                inputData.totalCols,
                inputData.keyBitLens.length,
                Arrays.toString(inputData.keyBitLens),
                MATERIALIZE_FULL_TABLE
            );

            FullTablePartyThread[] threads = IntStream.range(0, sortParties.length)
                .mapToObj(i -> new FullTablePartyThread(sortParties[i], permuteParties[i], inputData))
                .toArray(FullTablePartyThread[]::new);

            PeakMemoryMonitor memoryMonitor = new PeakMemoryMonitor(MEMORY_SAMPLE_INTERVAL_MILLIS);
            StopWatch stopWatch = new StopWatch();
            boolean watchRunning = false;

            try {
                memoryMonitor.start();
                stopWatch.start();
                watchRunning = true;

                Arrays.stream(threads).forEach(Thread::start);
                for (FullTablePartyThread thread : threads) {
                    thread.join();
                }

                stopWatch.stop();
                watchRunning = false;
            } finally {
                if (watchRunning) {
                    stopWatch.stop();
                }
                memoryMonitor.stop();
            }

            long time = stopWatch.getTime(TimeUnit.MILLISECONDS);
            long peakHeapBytes = memoryMonitor.getPeakUsedBytes();
            double peakHeapMB = peakHeapBytes / (1024.0 * 1024.0);

            long totalSendBytes = Arrays.stream(sortParties)
                .mapToLong(p -> p.getAbb3Party().getRpc().getSendByteLength())
                .sum();
            double totalSendKB = totalSendBytes / 1024.0;
            double totalSendMB = totalSendBytes / (1024.0 * 1024.0);

            verifyFullTable(inputData, threads[0].getPai(), threads[0].getSortedTable());

            Arrays.stream(sortParties).forEach(p -> new Thread(p::destroy).start());

            LOGGER.info(
                "-----test {}, rows={}, cols={}, keyNum={}, keyBitLens={}, materializeFullTable:{} end, time:{}ms, comm:{}B({}KB, {}MB), peakHeap:{}B({}MB)-----",
                sortParties[0].getPtoDesc().getPtoName(),
                inputData.rowNum,
                inputData.totalCols,
                inputData.keyBitLens.length,
                Arrays.toString(inputData.keyBitLens),
                MATERIALIZE_FULL_TABLE,
                time,
                totalSendBytes,
                String.format("%.2f", totalSendKB),
                String.format("%.4f", totalSendMB),
                peakHeapBytes,
                String.format("%.4f", peakHeapMB)
            );
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException(e);
        }
    }

    private static FileInputData readInputData(RuntimeConfig runtimeConfig) {
        Path inputPath = Path.of(runtimeConfig.inputPath);
        List<String> lines;
        try {
            lines = Files.readAllLines(inputPath);
        } catch (IOException e) {
            throw new IllegalArgumentException("Failed to read input file: " + inputPath, e);
        }

        List<String[]> rows = lines.stream()
            .map(String::trim)
            .filter(line -> !line.isEmpty())
            .filter(line -> !line.startsWith("#"))
            .map(line -> line.split("\\s+"))
            .toList();

        if (rows.isEmpty()) {
            throw new IllegalArgumentException("Input file is empty: " + inputPath);
        }

        int rowNum = rows.size();
        int totalCols = rows.get(0).length;
        if (runtimeConfig.keyNum <= 0 || runtimeConfig.keyNum > totalCols) {
            throw new IllegalArgumentException("pgsort.keyNum must be in [1, total column count]");
        }
        for (int i = 0; i < rows.size(); i++) {
            if (rows.get(i).length != totalCols) {
                throw new IllegalArgumentException("Inconsistent column count at line " + (i + 1));
            }
        }

        int[] keyBitLens = runtimeConfig.resolveKeyBitLens();
        BigInteger[] keyUppers = Arrays.stream(keyBitLens)
            .mapToObj(bitLen -> BigInteger.ONE.shiftLeft(bitLen))
            .toArray(BigInteger[]::new);
        BigInteger tableUpper = BigInteger.ONE.shiftLeft(runtimeConfig.bitLen);

        BigInteger[][] plainRows = new BigInteger[rowNum][totalCols];
        long[][] columns = new long[totalCols][rowNum];
        BigInteger[] compositeKeys = new BigInteger[rowNum];

        for (int rowIndex = 0; rowIndex < rowNum; rowIndex++) {
            String[] row = rows.get(rowIndex);
            BigInteger composite = BigInteger.ZERO;

            for (int colIndex = 0; colIndex < totalCols; colIndex++) {
                BigInteger value = new BigInteger(row[colIndex]);
                if (value.signum() < 0 || value.compareTo(tableUpper) >= 0) {
                    throw new IllegalArgumentException(
                        "Value out of range at row " + (rowIndex + 1)
                            + ", column " + (colIndex + 1)
                            + ": value=" + value
                            + ", bitLen=" + runtimeConfig.bitLen
                    );
                }

                if (colIndex < runtimeConfig.keyNum && value.compareTo(keyUppers[colIndex]) >= 0) {
                    throw new IllegalArgumentException(
                        "Key value out of range at row " + (rowIndex + 1)
                            + ", key column " + (colIndex + 1)
                            + ": value=" + value
                            + ", keyBitLen=" + keyBitLens[colIndex]
                    );
                }

                plainRows[rowIndex][colIndex] = value;
                columns[colIndex][rowIndex] = value.longValue();

                if (colIndex < runtimeConfig.keyNum) {
                    composite = composite.shiftLeft(keyBitLens[colIndex]).or(value);
                }
            }
            compositeKeys[rowIndex] = composite;
        }

        LongVector[] tableVectors = Arrays.stream(columns)
            .map(LongVector::create)
            .toArray(LongVector[]::new);

        LongVector[] keyVectors = Arrays.copyOfRange(tableVectors, 0, runtimeConfig.keyNum);

        return new FileInputData(
            inputPath.toString(),
            rowNum,
            totalCols,
            runtimeConfig.bitLen,
            keyBitLens,
            tableVectors,
            keyVectors,
            plainRows,
            compositeKeys
        );
    }

    private static void verifyFullTable(FileInputData inputData, int[] pai, LongVector[] sortedTable) {
        Assert.assertNotNull("sortedTable should be opened by party 0", sortedTable);
        Assert.assertEquals(inputData.totalCols, sortedTable.length);
        Assert.assertEquals(inputData.rowNum, pai.length);

        boolean[] seen = new boolean[pai.length];
        for (int value : pai) {
            Assert.assertTrue("Invalid permutation value: " + value, value >= 0 && value < pai.length);
            Assert.assertFalse("Duplicate permutation value: " + value, seen[value]);
            seen[value] = true;
        }

        long[][] sortedColumns = Arrays.stream(sortedTable)
            .map(LongVector::getElements)
            .toArray(long[][]::new);

        BigInteger lastKey = null;
        for (int sortedRow = 0; sortedRow < inputData.rowNum; sortedRow++) {
            BigInteger currentKey = BigInteger.ZERO;
            for (int keyIndex = 0; keyIndex < inputData.keyBitLens.length; keyIndex++) {
                BigInteger value = BigInteger.valueOf(sortedColumns[keyIndex][sortedRow]);
                currentKey = currentKey.shiftLeft(inputData.keyBitLens[keyIndex]).or(value);
            }
            if (lastKey != null) {
                Assert.assertTrue(lastKey.compareTo(currentKey) <= 0);
            }
            lastKey = currentKey;
        }

        for (int originalRow = 0; originalRow < inputData.rowNum; originalRow++) {
            int sortedPos = pai[originalRow];
            for (int col = 0; col < inputData.totalCols; col++) {
                BigInteger actual = BigInteger.valueOf(sortedColumns[col][sortedPos]);
                Assert.assertEquals(inputData.plainRows[originalRow][col], actual);
            }
        }
    }

    private static void completeNativeChecks(Abb3Party party) throws MpcAbortException {
        party.checkUnverified();
        party.getLongParty().checkUnverified();
        if (party instanceof Abb3RpParty rp && rp.getMacParty() != null
            && rp.getMacParty() != party.getLongParty()) {
            rp.getMacParty().checkUnverified();
        }
    }

    private static class ResidentMemoryMonitor {
        private final AtomicLong peak = new AtomicLong();
        private final AtomicBoolean running = new AtomicBoolean();
        private Thread worker;
        private volatile RuntimeException failure;

        private void sample() {
            try {
                for (String line : Files.readAllLines(Path.of("/proc/self/status"))) {
                    if (line.startsWith("VmRSS:")) {
                        long bytes = Long.parseLong(line.trim().split("\\s+")[1]) * 1024L;
                        peak.accumulateAndGet(bytes, Math::max);
                        return;
                    }
                }
                throw new IOException("VmRSS absent");
            } catch (IOException error) {
                throw new RuntimeException("Cannot measure sorting-window RSS", error);
            }
        }

        private void start() {
            sample();
            running.set(true);
            worker = new Thread(() -> {
                while (running.get()) {
                    try {
                        sample();
                        Thread.sleep(MEMORY_SAMPLE_INTERVAL_MILLIS);
                    } catch (InterruptedException stopped) {
                        return;
                    } catch (RuntimeException error) {
                        failure = error;
                        return;
                    }
                }
            }, "sort-window-rss");
            worker.setDaemon(true);
            worker.start();
        }

        private void stop() {
            running.set(false);
            worker.interrupt();
            try {
                worker.join();
            } catch (InterruptedException error) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(error);
            }
            sample();
            if (failure != null) throw failure;
        }
    }

    private static class FullTablePartyThread extends Thread {
        private static final Logger LOGGER = LoggerFactory.getLogger(FullTablePartyThread.class);

        private final PgSortParty sortParty;
        private final PermuteParty permuteParty;
        private final FileInputData inputData;

        private int[] pai;
        private LongVector[] sortedTable;
        private long sortOnlyTimeMs;
        private long sortOnlySendBytes;
        private long sortOnlyPeakRssBytes;
        private volatile Throwable failure;

        FullTablePartyThread(PgSortParty sortParty, PermuteParty permuteParty, FileInputData inputData) {
            this.sortParty = sortParty;
            this.permuteParty = permuteParty;
            this.inputData = inputData;
        }

        int[] getPai() {
            return pai;
        }

        LongVector[] getSortedTable() {
            return sortedTable;
        }

        long getSortOnlyTimeMs() {
            return sortOnlyTimeMs;
        }

        long getSortOnlySendBytes() {
            return sortOnlySendBytes;
        }

        @Override
        public void run() {
            int[] keyBitLens;
            LongVector[] plainTable = null;

            if (sortParty.getRpc().ownParty().getPartyId() == 0) {
                keyBitLens = inputData.keyBitLens;
                plainTable = inputData.tableVectors;

                List<byte[]> send = List.of(IntUtils.intArrayToByteArray(keyBitLens));
                DataPacketHeader to1 = new DataPacketHeader(0, 0, 0, 0, 1);
                DataPacketHeader to2 = new DataPacketHeader(0, 0, 0, 0, 2);
                sortParty.getRpc().send(DataPacket.fromByteArrayList(to1, send));
                sortParty.getRpc().send(DataPacket.fromByteArrayList(to2, send));
            } else {
                DataPacketHeader from0 = new DataPacketHeader(
                    0,
                    0,
                    0,
                    0,
                    sortParty.getRpc().ownParty().getPartyId()
                );
                keyBitLens = IntUtils.byteArrayToIntArray(sortParty.getRpc().receive(from0).getPayload().get(0));
            }

            try {
                long[] sortCost = sortParty.setUsage(
                    new PgSortFnParam(PgSortOp.SORT_A, inputData.rowNum, keyBitLens)
                );
                long[] permuteCost = permuteParty.setUsage(
                    new PermuteFnParam(PermuteOp.APPLY_INV_A_A, inputData.rowNum, inputData.totalCols, 1)
                );
                long[] costTuple = new long[]{
                    sortCost[0] + permuteCost[0],
                    sortCost[1] + permuteCost[1]
                };

                long stageStartNano = System.nanoTime();
                long stageSendBefore = sortParty.getAbb3Party().getRpc().getSendByteLength();
                sortParty.getAbb3Party().init();
                sortParty.init();
                permuteParty.init();
                logBitonicProfileStage("INIT_PARTIES", 98, stageStartNano, stageSendBefore);

                TripletLongVector[] tableShare;
                stageStartNano = System.nanoTime();
                stageSendBefore = sortParty.getAbb3Party().getRpc().getSendByteLength();
                if (sortParty.getRpc().ownParty().getPartyId() == 0) {
                    tableShare = (TripletLongVector[]) sortParty.getAbb3Party().getLongParty().shareOwn(plainTable);
                } else {
                    int[] nums = IntStream.range(0, inputData.totalCols)
                        .map(i -> inputData.rowNum)
                        .toArray();
                    tableShare = (TripletLongVector[]) sortParty.getAbb3Party().getLongParty()
                        .shareOther(nums, sortParty.getRpc().getParty(0));
                }
                logBitonicProfileStage("SHARE_INPUT_TABLE", 99, stageStartNano, stageSendBefore);

                TripletLongVector[] keyShare = Arrays.copyOfRange(tableShare, 0, keyBitLens.length);

                StopWatch stopWatch = new StopWatch();
                completeNativeChecks(sortParty.getAbb3Party());
                ResidentMemoryMonitor sortMemory = new ResidentMemoryMonitor();
                sortMemory.start();
                long sortOnlySendBefore = sortParty.getAbb3Party().getRpc().getSendByteLength();
                stopWatch.start();

                TripletLongVector paiShare = sortParty.perGen4MultiDim(keyShare, keyBitLens);

                stageStartNano = System.nanoTime();
                stageSendBefore = sortParty.getAbb3Party().getRpc().getSendByteLength();
                TripletLongVector[] sortedTableShare = permuteParty.applyInvPermutation(paiShare, tableShare);
                logBitonicProfileStage("FULL_TABLE_APPLY_INV_PERM", 102, stageStartNano, stageSendBefore);

                completeNativeChecks(sortParty.getAbb3Party());
                stopWatch.stop();
                sortMemory.stop();
                sortOnlyPeakRssBytes = sortMemory.peak.get();
                long sortOnlySendAfter = sortParty.getAbb3Party().getRpc().getSendByteLength();
                sortOnlyTimeMs = stopWatch.getTime(TimeUnit.MILLISECONDS);
                sortOnlySendBytes = Math.max(0L, sortOnlySendAfter - sortOnlySendBefore);

                LOGGER.info(
                    "P{} keyBitLens:{}, full-table sort-only time:{}ms, sort-only comm:{}B",
                    sortParty.getRpc().ownParty().getPartyId(),
                    Arrays.toString(keyBitLens),
                    sortOnlyTimeMs,
                    sortOnlySendBytes
                );

                stageStartNano = System.nanoTime();
                stageSendBefore = sortParty.getAbb3Party().getRpc().getSendByteLength();
                long[] paiLong = sortParty.getAbb3Party().getLongParty().open(paiShare)[0].getElements();
                pai = Arrays.stream(paiLong).mapToInt(each -> (int) each).toArray();

                LongVector[] openedTable = sortParty.getAbb3Party().getLongParty().open(sortedTableShare);
                logBitonicProfileStage("OPEN_RESULTS", 103, stageStartNano, stageSendBefore);
                if (sortParty.getRpc().ownParty().getPartyId() == 0) {
                    sortedTable = openedTable;
                }

                long usedBitTuple = costTuple[0] == 0
                    ? 0
                    : sortParty.getAbb3Party().getTripletProvider().getZ2MtProvider().getAllTupleNum();
                long usedLongTuple = costTuple[1] == 0
                    ? 0
                    : sortParty.getAbb3Party().getTripletProvider().getZl64MtProvider().getAllTupleNum();

                LOGGER.info(
                    "computed bitTupleNum:{}, actually used bitTupleNum:{} | computed longTupleNum:{}, actually used longTupleNum:{}",
                    costTuple[0],
                    usedBitTuple,
                    costTuple[1],
                    usedLongTuple
                );
            } catch (MpcAbortException e) {
                throw new RuntimeException(e);
            }
        }

        private void logBitonicProfileStage(String stage, int stageId, long startNano, long sendBefore) {
            long elapsedNano = System.nanoTime() - startNano;
            long sendAfter = sortParty.getAbb3Party().getRpc().getSendByteLength();
            long deltaSendBytes = sendAfter >= sendBefore ? sendAfter - sendBefore : -1L;
            double elapsedSeconds = elapsedNano / 1_000_000_000.0;
            double deltaSendMB = deltaSendBytes < 0 ? -1.0 : deltaSendBytes / (1024.0 * 1024.0);
            LOGGER.info(
                "RESULT_BITONIC_EXTRA_CSV,party={},stage={},stageId={},time_s={},comm_MB={}",
                sortParty.getRpc().ownParty().getPartyId(),
                stage,
                stageId,
                String.format("%.9f", elapsedSeconds),
                String.format("%.9f", deltaSendMB)
            );
        }
    }

    private static class PeakMemoryMonitor {
        private final long intervalMillis;
        private final AtomicBoolean running;
        private final AtomicLong peakUsedBytes;
        private Thread worker;

        private PeakMemoryMonitor(long intervalMillis) {
            this.intervalMillis = intervalMillis;
            this.running = new AtomicBoolean(false);
            this.peakUsedBytes = new AtomicLong(currentUsedBytes());
        }

        private void start() {
            running.set(true);
            updatePeak();

            worker = new Thread(() -> {
                while (running.get()) {
                    updatePeak();
                    try {
                        Thread.sleep(intervalMillis);
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                        break;
                    }
                }
                updatePeak();
            }, "peak-memory-monitor");

            worker.setDaemon(true);
            worker.start();
        }

        private void stop() {
            running.set(false);
            if (worker != null) {
                worker.interrupt();
                try {
                    worker.join();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            updatePeak();
        }

        private long getPeakUsedBytes() {
            return peakUsedBytes.get();
        }

        private void updatePeak() {
            peakUsedBytes.accumulateAndGet(currentUsedBytes(), Math::max);
        }

        private static long currentUsedBytes() {
            Runtime runtime = Runtime.getRuntime();
            return runtime.totalMemory() - runtime.freeMemory();
        }
    }

    private static class ProtocolParties {
        private final PgSortParty[] sortParties;
        private final PermuteParty[] permuteParties;

        private ProtocolParties(PgSortParty[] sortParties, PermuteParty[] permuteParties) {
            this.sortParties = sortParties;
            this.permuteParties = permuteParties;
        }
    }

    private static class FileInputData {
        private final String inputPath;
        private final int rowNum;
        private final int totalCols;
        private final int tableBitLen;
        private final int[] keyBitLens;
        private final LongVector[] tableVectors;
        private final LongVector[] keyVectors;
        private final BigInteger[][] plainRows;
        private final BigInteger[] compositeKeys;

        private FileInputData(
            String inputPath,
            int rowNum,
            int totalCols,
            int tableBitLen,
            int[] keyBitLens,
            LongVector[] tableVectors,
            LongVector[] keyVectors,
            BigInteger[][] plainRows,
            BigInteger[] compositeKeys
        ) {
            this.inputPath = inputPath;
            this.rowNum = rowNum;
            this.totalCols = totalCols;
            this.tableBitLen = tableBitLen;
            this.keyBitLens = keyBitLens;
            this.tableVectors = tableVectors;
            this.keyVectors = keyVectors;
            this.plainRows = plainRows;
            this.compositeKeys = compositeKeys;
        }
    }

    private static class RuntimeConfig {
        private final String inputPath;
        private final int algorithm;
        private final String security;
        private final int keyNum;
        private final int bitLen;
        private final String bitLens;
        private final boolean parallel;
        private final boolean stable;
        private final boolean baseUseMac;

        private RuntimeConfig(
            String inputPath,
            int algorithm,
            String security,
            int keyNum,
            int bitLen,
            String bitLens,
            boolean parallel,
            boolean stable,
            boolean baseUseMac
        ) {
            this.inputPath = inputPath;
            this.algorithm = algorithm;
            this.security = security;
            this.keyNum = keyNum;
            this.bitLen = bitLen;
            this.bitLens = bitLens;
            this.parallel = parallel;
            this.stable = stable;
            this.baseUseMac = baseUseMac;
        }

        private static RuntimeConfig fromSystemProperties() {
            String inputPath = requireProperty("pgsort.input");
            int algorithm = getIntProperty("pgsort.algorithm", 1);
            String security = getStringProperty("pgsort.security", "semi-honest")
                .toLowerCase(Locale.ROOT)
                .replace("_", "-");
            int keyNum = getIntProperty("pgsort.keyNum", 1);
            int bitLen = getIntProperty("pgsort.bitLen", 32);
            String bitLens = System.getProperty("pgsort.bitLens");
            boolean parallel = getBooleanProperty("pgsort.parallel", false);
            boolean stable = getBooleanProperty("pgsort.stable", false);
            boolean baseUseMac = getBooleanProperty("pgsort.baseUseMac", false);

            if (algorithm < 1 || algorithm > 3) {
                throw new IllegalArgumentException("pgsort.algorithm must be 1(bitonic), 2(quick), or 3(radix)");
            }
            if (!security.equals("semi-honest") && !security.equals("malicious")) {
                throw new IllegalArgumentException("pgsort.security must be semi-honest or malicious");
            }
            if (keyNum <= 0) {
                throw new IllegalArgumentException("pgsort.keyNum must be positive");
            }
            if (bitLen <= 0 || bitLen > 64) {
                throw new IllegalArgumentException("pgsort.bitLen must be in [1, 64]");
            }

            return new RuntimeConfig(
                inputPath,
                algorithm,
                security,
                keyNum,
                bitLen,
                bitLens,
                parallel,
                stable,
                baseUseMac
            );
        }

        private int[] resolveKeyBitLens() {
            if (bitLens == null || bitLens.trim().isEmpty()) {
                int[] result = new int[keyNum];
                Arrays.fill(result, bitLen);
                return result;
            }

            int[] result = Arrays.stream(bitLens.split(","))
                .map(String::trim)
                .mapToInt(Integer::parseInt)
                .toArray();

            if (result.length != keyNum) {
                throw new IllegalArgumentException("pgsort.bitLens length must equal pgsort.keyNum");
            }
            for (int each : result) {
                if (each <= 0 || each > 64) {
                    throw new IllegalArgumentException("Each pgsort.bitLens value must be in [1, 64]");
                }
            }
            return result;
        }

        private static String requireProperty(String name) {
            String value = System.getProperty(name);
            if (value == null || value.trim().isEmpty()) {
                throw new IllegalArgumentException("Missing required Maven property: -D" + name + "=...");
            }
            return value.trim();
        }

        private static String getStringProperty(String name, String defaultValue) {
            String value = System.getProperty(name);
            return value == null || value.trim().isEmpty() ? defaultValue : value.trim();
        }

        private static int getIntProperty(String name, int defaultValue) {
            String value = System.getProperty(name);
            return value == null || value.trim().isEmpty() ? defaultValue : Integer.parseInt(value.trim());
        }

        private static boolean getBooleanProperty(String name, boolean defaultValue) {
            String value = System.getProperty(name);
            return value == null || value.trim().isEmpty() ? defaultValue : Boolean.parseBoolean(value);
        }
    }
}
