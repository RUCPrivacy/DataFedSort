package edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.bitonic;

import edu.alibaba.mpc4j.common.circuit.z2.MpcZ2Vector;
import edu.alibaba.mpc4j.common.circuit.z2.Z2CircuitConfig;
import edu.alibaba.mpc4j.common.circuit.z2.Z2IntegerCircuit;
import edu.alibaba.mpc4j.common.circuit.z2.comparator.ComparatorFactory;
import edu.alibaba.mpc4j.common.circuit.z2.comparator.ComparatorFactory.ComparatorType;
import edu.alibaba.mpc4j.common.circuit.z2.psorter.bitonic.PermutableBitonicSorter;
import edu.alibaba.mpc4j.common.circuit.z2.utils.Z2VectorUtils;
import edu.alibaba.mpc4j.common.rpc.MpcAbortException;
import edu.alibaba.mpc4j.common.rpc.PtoState;
import edu.alibaba.mpc4j.common.structure.vector.LongVector;
import edu.alibaba.mpc4j.common.tool.bitvector.BitVector;
import edu.alibaba.mpc4j.common.tool.utils.CommonUtils;
import edu.alibaba.mpc4j.common.tool.utils.LongUtils;
import edu.alibaba.mpc4j.s3pc.abb3.basic.Abb3Party;
import edu.alibaba.mpc4j.s3pc.abb3.basic.conversion.ConvOperations.ConvOp;
import edu.alibaba.mpc4j.s3pc.abb3.structure.z2.TripletZ2Vector;
import edu.alibaba.mpc4j.s3pc.abb3.structure.zlong.TripletLongVector;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteFactory;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteOperations.PermuteFnParam;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteOperations.PermuteOp;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.permutation.PermuteParty;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.AbstractPgSortParty;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.PgSortOperations.PgSortFnParam;
import edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.PgSortParty;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Arrays;
import java.util.concurrent.TimeUnit;
import java.util.stream.LongStream;

/**
 * oblivious bitonic sorting party
 *
 * @author Feng Han
 * @date 2024/02/26
 */
public class BitonicPgSortParty extends AbstractPgSortParty implements PgSortParty {
    private static final Logger PROFILE_LOGGER = LoggerFactory.getLogger(BitonicPgSortParty.class);
    /**
     * adder type
     */
    public final ComparatorType comparatorType;
    /**
     * bitonic sorter
     */
    public PermutableBitonicSorter sorter;
    /**
     * permute party
     */
    public PermuteParty permuteParty;
    /**
     * whether the sorting process is stable
     */
    private final boolean isStable;

    public BitonicPgSortParty(Abb3Party abb3Party, BitonicPgSortConfig config) {
        super(BitonicPgSortPtoDesc.getInstance(), abb3Party, config);
        comparatorType = config.getComparatorTypes();
        sorter = new PermutableBitonicSorter(new Z2IntegerCircuit(z2cParty,
            new Z2CircuitConfig.Builder().setComparatorType(comparatorType).build()));
        sorter.setRoundMetricContext(
            () -> abb3Party.getRpc().getSendByteLength(),
            () -> abb3Party.getRpc().ownParty().getPartyId()
        );
        permuteParty = PermuteFactory.createParty(abb3Party, config.getPermuteConfig());
        isStable = config.isStable();
        addMultiSubPto(permuteParty);
    }

    @Override
    public void init() throws MpcAbortException {
        logPhaseInfo(PtoState.INIT_BEGIN);

        stopWatch.start();
        permuteParty.init();
        initState();
        stopWatch.stop();
        long initTime = stopWatch.getTime(TimeUnit.MILLISECONDS);
        stopWatch.reset();
        logStepInfo(PtoState.INIT_STEP, 1, 1, initTime);

        logPhaseInfo(PtoState.INIT_END);
    }

    @Override
    public long[] setUsage(PgSortFnParam... params) {
        if (!isMalicious) {
            return new long[]{0, 0};
        }
        long bitTupleNum = 0, longTupleNum = 0;
        for (PgSortFnParam funParam : params) {
            int dataNum = CommonUtils.getByteLength(funParam.dataNum) << 3;
            int sumBitNum = funParam.op.name().endsWith("_A") ? Arrays.stream(funParam.dims).sum() : funParam.dims[0];

            int logM = LongUtils.ceilLog2(dataNum);
            int dataExtendNum = CommonUtils.getByteLength(dataNum) << 3;
            long compareNum = (long) logM * (logM + 1) / 2 * dataExtendNum / 2;
            bitTupleNum += compareNum * (sumBitNum + logM + ComparatorFactory.getAndGateNum(comparatorType, sumBitNum + logM));

            if (funParam.op.name().endsWith("_A")) {
                for (int bit : funParam.dims) {
                    bitTupleNum += abb3Party.getConvParty().getTupleNum(ConvOp.A2B, dataNum, 1, bit)[0];
                }
                bitTupleNum += abb3Party.getConvParty().getTupleNum(ConvOp.B2A, dataNum, 1, 64)[0];
            } else {
                permuteParty.setUsage(new PermuteFnParam(PermuteOp.APPLY_INV_B_B, dataNum, logM, logM));
            }
        }
        abb3Party.updateNum(bitTupleNum, longTupleNum);
        return new long[]{bitTupleNum, longTupleNum};
    }

    @Override
    public TripletLongVector perGen4MultiDim(TripletLongVector[] input, int[] bitLens) throws MpcAbortException {
        int totalBitNum = Arrays.stream(bitLens).sum();
        TripletZ2Vector[] saveSortRes = new TripletZ2Vector[totalBitNum];
        return perGen4MultiDimWithOrigin(input, bitLens, saveSortRes);
    }

    @Override
    public TripletLongVector perGen4MultiDimWithOrigin(TripletLongVector[] input, int[] bitLens, TripletZ2Vector[] saveSortRes) throws MpcAbortException {
        checkInput(input, bitLens, saveSortRes);
        logPhaseInfo(PtoState.PTO_BEGIN, "perGen4MultiDimWithOrigin");

        long stageStartNano = System.nanoTime();
        long stageSendBefore = abb3Party.getRpc().getSendByteLength();
        stopWatch.start();
        for (int i = 0, start = 0; i < input.length; i++) {
            System.arraycopy(abb3Party.getConvParty().a2b(input[i], bitLens[i]), 0, saveSortRes, start, bitLens[i]);
            start += bitLens[i];
        }
        logBitonicExtraStage("A2B_KEYS", 100, stageStartNano, stageSendBefore);
        logStepInfo(PtoState.PTO_STEP, 1, 3, resetAndGetTime());

        stopWatch.start();
        MpcZ2Vector[] tmpPerm = sorter.sort(new MpcZ2Vector[][]{saveSortRes}, true, isStable);
        TripletZ2Vector[] perm = Arrays.stream(tmpPerm).map(ea -> (TripletZ2Vector) ea).toArray(TripletZ2Vector[]::new);
        logStepInfo(PtoState.PTO_STEP, 2, 3, resetAndGetTime());

        stageStartNano = System.nanoTime();
        stageSendBefore = abb3Party.getRpc().getSendByteLength();
        stopWatch.start();
        TripletLongVector index = (TripletLongVector) abb3Party.getLongParty().setPublicValue(LongVector.create(LongStream.range(0, perm[0].getNum()).toArray()));
        TripletLongVector permA = abb3Party.getConvParty().b2a(perm);
        TripletLongVector permRes = permuteParty.applyInvPermutation(permA, index)[0];
        logBitonicExtraStage("B2A_AND_PERM_INDEX", 101, stageStartNano, stageSendBefore);
        logStepInfo(PtoState.PTO_STEP, 3, 3, resetAndGetTime());

        logPhaseInfo(PtoState.PTO_END, "perGen4MultiDimWithOrigin");
        return permRes;
    }

    private void logBitonicExtraStage(String stage, int stageId, long startNano, long sendBefore) {
        long elapsedNano = System.nanoTime() - startNano;
        long sendAfter = abb3Party.getRpc().getSendByteLength();
        long deltaSendBytes = sendAfter >= sendBefore ? sendAfter - sendBefore : -1L;
        double elapsedSeconds = elapsedNano / 1_000_000_000.0;
        double deltaSendMB = deltaSendBytes < 0 ? -1.0 : deltaSendBytes / (1024.0 * 1024.0);
        PROFILE_LOGGER.info(
            "RESULT_BITONIC_EXTRA_CSV,party={},stage={},stageId={},time_s={},comm_MB={}",
            abb3Party.getRpc().ownParty().getPartyId(),
            stage,
            stageId,
            String.format("%.9f", elapsedSeconds),
            String.format("%.9f", deltaSendMB)
        );
    }

    @Override
    public TripletZ2Vector[] perGen(TripletZ2Vector[] input) throws MpcAbortException {
        TripletZ2Vector[] tmp = Arrays.copyOf(input, input.length);
        return perGenAndSortOrigin(tmp);
    }

    @Override
    public TripletZ2Vector[] perGenAndSortOrigin(TripletZ2Vector[] input) throws MpcAbortException {
        logPhaseInfo(PtoState.PTO_BEGIN, "perGenAndSortOrigin");

        stopWatch.start();
        MpcZ2Vector[] tmpPerm = sorter.sort(new MpcZ2Vector[][]{input}, true, isStable);
        TripletZ2Vector[] perm = Arrays.stream(tmpPerm).map(ea -> (TripletZ2Vector) ea).toArray(TripletZ2Vector[]::new);
        logStepInfo(PtoState.PTO_STEP, 1, 2, resetAndGetTime());

        stopWatch.start();
        TripletZ2Vector[] permRes;
        if (tmpPerm[0].bitNum() > 1) {
            BitVector[] index = Z2VectorUtils.getBinaryIndex(perm[0].bitNum());
            TripletZ2Vector[] shareIndex = (TripletZ2Vector[]) z2cParty.setPublicValues(index);
            permRes = permuteParty.applyInvPermutation(perm, shareIndex);
        } else {
            permRes = (TripletZ2Vector[]) tmpPerm;
        }
        logStepInfo(PtoState.PTO_STEP, 2, 2, resetAndGetTime());

        logPhaseInfo(PtoState.PTO_END, "perGenAndSortOrigin");
        return permRes;
    }
}
