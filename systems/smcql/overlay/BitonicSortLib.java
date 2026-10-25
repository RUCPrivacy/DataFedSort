// Copyright (C) 2014 by Xiao Shaun Wang <wangxiao@cs.umd.edu>
package com.oblivm.backend.circuits;

import com.oblivm.backend.circuits.arithmetic.IntegerLib;
import com.oblivm.backend.flexsc.CompEnv;

import java.lang.reflect.Field;
import java.util.Map;
import java.util.TreeMap;

public class BitonicSortLib<T> extends IntegerLib<T> {
	/**
	 * 
	 */
	private static final long serialVersionUID = 548013917964346826L;
	private static final boolean PROFILE_ROUNDS =
			Boolean.parseBoolean(System.getProperty("smcql.profileRounds", "false"));

	private final TreeMap<Integer, RoundStats> roundStats = new TreeMap<Integer, RoundStats>();

	public BitonicSortLib(CompEnv<T> e) {
		super(e);
	}

	public void sortWithPayload(T[][] a, T[][] data, T isAscending) {
		roundStats.clear();
		bitonicSortWithPayload(a, data, 0, a.length, isAscending);
		printRoundProfile();
	}

	private void bitonicSortWithPayload(T[][] key, T[][] data, int lo, int n,
			T dir) {
		if (n > 1) {
			int m = n / 2;
			bitonicSortWithPayload(key, data, lo, m, not(dir));
			bitonicSortWithPayload(key, data, lo + m, n - m, dir);
			bitonicMergeWithPayload(key, data, lo, n, dir);
		}
	}

	protected void bitonicMergeWithPayload(T[][] key, T[][] data, int lo, int n,
			T dir) {
		if (n > 1) {
			int m = greatestPowerOfTwoLessThan(n);
			long start = 0L;
			long commBefore = -1L;
			if (PROFILE_ROUNDS) {
				env.flush();
				commBefore = readCommunicationBytes();
				start = System.nanoTime();
			}
			for (int i = lo; i < lo + n - m; i++)
				compareWithPayload(key, data, i, i + m, dir);
			if (PROFILE_ROUNDS) {
				env.flush();
				long elapsed = System.nanoTime() - start;
				long commAfter = readCommunicationBytes();
				long commDelta = commBefore >= 0 && commAfter >= commBefore ? commAfter - commBefore : -1L;
				recordRound(n, n - m, elapsed, commDelta);
			}
			bitonicMergeWithPayload(key, data, lo, m, dir);
			bitonicMergeWithPayload(key, data, lo + m, n - m, dir);
		}
	}

	private void compareWithPayload(T[][] key, T[][] data, int i, int j, T dir) {
		T greater = not(leq(key[i], key[j]));
		T swap = eq(greater, dir);
		T[] s = mux(key[j], key[i], swap);
		s = xor(s, key[i]);
		T[] ki = xor(key[j], s);
		T[] kj = xor(key[i], s);
		key[i] = ki;
		key[j] = kj;

		T[] s2 = mux(data[j], data[i], swap);
		s2 = xor(s2, data[i]);
		T[] di = xor(data[j], s2);
		T[] dj = xor(data[i], s2);
		data[i] = di;
		data[j] = dj;
	}

	public void sort(T[][] a, T isAscending) {
		bitonicSort(a, 0, a.length, isAscending);
	}
	
	public void merge(T[][] a, T isAscending) {
		bitonicMerge(a, 0, a.length, isAscending);
	}

	private void bitonicSort(T[][] key, int lo, int n, T dir) {
		if (n > 1) {
			int m = n / 2;
			bitonicSort(key, lo, m, not(dir));
			bitonicSort(key, lo + m, n - m, dir);
			bitonicMerge(key, lo, n, dir);
		}
	}

	protected void bitonicMerge(T[][] key, int lo, int n, T dir) {
		if (n > 1) {
			int m = greatestPowerOfTwoLessThan(n);
			for (int i = lo; i < lo + n - m; i++)
				compare(key, i, i + m, dir);
			bitonicMerge(key, lo, m, dir);
			bitonicMerge(key, lo + m, n - m, dir);
		}
	}

	private void compare(T[][] key, int i, int j, T dir) {
		T swap = eq(not(leq(key[i], key[j])), dir);
		T[] s = mux(key[j], key[i], swap);
		s = xor(s, key[i]);
		T[] ki = xor(key[j], s);
		T[] kj = xor(key[i], s);
		key[i] = ki;
		key[j] = kj;
	}

	private int greatestPowerOfTwoLessThan(int n) {
		int k = 1;
		while (k < n)
			k = k << 1;
		return k >> 1;
	}

	private void recordRound(int mergeSize, int compares, long elapsedNanos, long commBytes) {
		RoundStats stats = roundStats.get(mergeSize);
		if (stats == null) {
			stats = new RoundStats();
			roundStats.put(mergeSize, stats);
		}
		stats.calls++;
		stats.compares += compares;
		stats.timeNanos += elapsedNanos;
		if (commBytes >= 0) {
			stats.commBytes += commBytes;
			stats.hasComm = true;
		}
	}

	private void printRoundProfile() {
		if (!PROFILE_ROUNDS || roundStats.isEmpty() || readCommunicationBytes() < 0) {
			return;
		}
		System.out.println("ROUND_PROFILE_CSV_HEADER,merge_size,calls,compares,time_s,comm_MB");
		for (Map.Entry<Integer, RoundStats> entry : roundStats.entrySet()) {
			RoundStats stats = entry.getValue();
			double timeSeconds = stats.timeNanos / 1000000000.0;
			double commMB = stats.hasComm ? stats.commBytes / (1024.0 * 1024.0) : -1.0;
			System.out.println("ROUND_PROFILE_CSV," + entry.getKey() + "," + stats.calls + ","
					+ stats.compares + "," + timeSeconds + "," + commMB);
		}
	}

	private long readCommunicationBytes() {
		try {
			Field cosField = env.channel.getClass().getDeclaredField("cos");
			cosField.setAccessible(true);
			Object cos = cosField.get(env.channel);

			Field cisField = env.channel.getClass().getDeclaredField("cis");
			cisField.setAccessible(true);
			Object cis = cisField.get(env.channel);

			if (cos == null || cis == null) {
				return -1L;
			}

			long sent = ((Number) cos.getClass().getMethod("getByteCount").invoke(cos)).longValue();
			long received = ((Number) cis.getClass().getMethod("getByteCount").invoke(cis)).longValue();
			return sent + received;
		} catch (Exception e) {
			return -1L;
		}
	}

	private static final class RoundStats {
		long calls = 0L;
		long compares = 0L;
		long timeNanos = 0L;
		long commBytes = 0L;
		boolean hasComm = false;
	}
	
	public void sortWithPayload(T[] a, T[][] data, T isAscending) {
		bitonicSortWithPayload(a, data, 0, a.length, isAscending);
	}

	private void bitonicSortWithPayload(T[] key, T[][] data, int lo, int n,
			T dir) {
		if (n > 1) {
			int m = n / 2;
			bitonicSortWithPayload(key, data, lo, m, not(dir));
			bitonicSortWithPayload(key, data, lo + m, n - m, dir);
			bitonicMergeWithPayload(key, data, lo, n, dir);
		}
	}

	private void bitonicMergeWithPayload(T[] key, T[][] data, int lo, int n,
			T dir) {
		if (n > 1) {
			int m = greatestPowerOfTwoLessThan(n);
			for (int i = lo; i < lo + n - m; i++)
				compareWithPayload(key, data, i, i + m, dir);
			bitonicMergeWithPayload(key, data, lo, m, dir);
			bitonicMergeWithPayload(key, data, lo + m, n - m, dir);
		}
	}

	private void compareWithPayload(T[] key, T[][] data, int i, int j, T dir) {
		T greater = and(key[i], not(key[j]));
		T swap = eq(greater, dir);
		T s = mux(key[j], key[i], swap);
		s = xor(s, key[i]);
		T ki = xor(key[j], s);
		T kj = xor(key[i], s);
		key[i] = ki;
		key[j] = kj;

		T[] s2 = mux(data[j], data[i], swap);
		s2 = xor(s2, data[i]);
		T[] di = xor(data[j], s2);
		T[] dj = xor(data[i], s2);
		data[i] = di;
		data[j] = dj;
	}
	
	public void sortWithPayload(T[][] a, T[] data, T isAscending) {
		bitonicSortWithPayload(a, data, 0, a.length, isAscending);
	}

	private void bitonicSortWithPayload(T[][] key, T[] data, int lo, int n,
			T dir) {
		if (n > 1) {
			int m = n / 2;
			bitonicSortWithPayload(key, data, lo, m, not(dir));
			bitonicSortWithPayload(key, data, lo + m, n - m, dir);
			bitonicMergeWithPayload(key, data, lo, n, dir);
		}
	}

	private void bitonicMergeWithPayload(T[][] key, T[] data, int lo, int n,
			T dir) {
		if (n > 1) {
			int m = greatestPowerOfTwoLessThan(n);
			for (int i = lo; i < lo + n - m; i++)
				compareWithPayload(key, data, i, i + m, dir);
			bitonicMergeWithPayload(key, data, lo, m, dir);
			bitonicMergeWithPayload(key, data, lo + m, n - m, dir);
		}
	}

	private void compareWithPayload(T[][] key, T[] data, int i, int j, T dir) {
		T greater = not(leq(key[i], key[j]));
		T swap = eq(greater, dir);
		T[] s = mux(key[j], key[i], swap);
		s = xor(s, key[i]);
		T[] ki = xor(key[j], s);
		T[] kj = xor(key[i], s);
		key[i] = ki;
		key[j] = kj;

		T s2 = mux(data[j], data[i], swap);
		s2 = xor(s2, data[i]);
		T di = xor(data[j], s2);
		T dj = xor(data[i], s2);
		data[i] = di;
		data[j] = dj;
	}
	

}
