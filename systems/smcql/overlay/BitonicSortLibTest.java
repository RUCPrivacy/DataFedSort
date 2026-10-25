package com.oblivm.backend.circuits;

import com.oblivm.backend.flexsc.CompEnv;
import com.oblivm.backend.flexsc.Flag;
import com.oblivm.backend.gc.regular.GCEva;
import com.oblivm.backend.gc.regular.GCGen;
import com.oblivm.backend.network.Client;
import com.oblivm.backend.network.Server;
import junit.framework.TestCase;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

public class BitonicSortLibTest extends TestCase {

	private static final int DEFAULT_PORT = 12345;

	public static void main(String[] args) throws Exception {
		new BitonicSortLibTest().testBitonicSortWithPayload();
	}

	public void testBitonicSortWithPayload() throws Exception {
		String input = System.getProperty("input", "");
		int numKeys = Integer.parseInt(System.getProperty("numKeys", System.getProperty("m", "1")));
		int colBits = Integer.parseInt(System.getProperty("colBits", "32"));
		String dataType = System.getProperty("dataType", "int32");
		int port = Integer.parseInt(System.getProperty("port", String.valueOf(DEFAULT_PORT)));
		String role = System.getProperty("smcql.role", "both").toLowerCase();
		String host = System.getProperty("smcql.host", "localhost");

		if (input.trim().isEmpty()) {
			throw new IllegalArgumentException("Set -Dinput to a TPC-H-derived input file");
		}
		TableInput table = TableInput.read(input);

		if (numKeys <= 0 || numKeys > table.cols) {
			throw new IllegalArgumentException("numKeys must satisfy 1 <= numKeys <= input columns");
		}
		if (colBits <= 0 || colBits > 64) {
			throw new IllegalArgumentException("colBits must satisfy 1 <= colBits <= 64");
		}

		Flag.countIO = true;
		Flag.CountTime = true;

		MemoryPeakTracker memoryTracker = new MemoryPeakTracker();
		memoryTracker.start();

		Server server = new Server();
		setThreadedIO(server, false);

		Client client = new Client();
		setThreadedIO(client, false);

		final long[] commCost = new long[] { -1L };
		final double[] sortSeconds = new double[] { 0.0 };
		final Throwable[] threadFailure = new Throwable[1];

		Runnable serverTask = () -> {
			try {
				server.listen(port);
				GCGen env = new GCGen(server);
				runSort(env, table, numKeys, colBits, true, null, sortSeconds);
			} catch (Throwable t) {
				threadFailure[0] = t;
			} finally {
				server.disconnect();
			}
		};

		Runnable clientTask = () -> {
			try {
				Thread.sleep(Long.parseLong(System.getProperty("smcql.clientDelayMs", "500")));
				client.connect(host, port);
				GCEva env = new GCEva(client);
				runSort(env, table, numKeys, colBits, false, commCost, sortSeconds);
			} catch (InterruptedException e) {
				Thread.currentThread().interrupt();
				threadFailure[0] = e;
			} catch (Throwable t) {
				threadFailure[0] = t;
			} finally {
				client.disconnect();
			}
		};

		long wallStart = System.nanoTime();
		if ("server".equals(role) || "alice".equals(role)) {
			serverTask.run();
		} else if ("client".equals(role) || "bob".equals(role)) {
			clientTask.run();
		} else if ("both".equals(role)) {
			Thread serverThread = new Thread(serverTask);
			Thread clientThread = new Thread(clientTask);
			serverThread.start();
			clientThread.start();
			serverThread.join();
			clientThread.join();
		} else {
			throw new IllegalArgumentException("smcql.role must be server, client, or both");
		}
		long wallEnd = System.nanoTime();
		memoryTracker.stop();
		if (threadFailure[0] != null) {
			throw new RuntimeException("SMCQL benchmark thread failed", threadFailure[0]);
		}

		double wallSeconds = (wallEnd - wallStart) / 1000000000.0;
		double commMB = commCost[0] >= 0 ? commCost[0] / (1024.0 * 1024.0) : -1.0;
		double peakMemoryMB = memoryTracker.peakBytes() / (1024.0 * 1024.0);

		System.out.println("\n========== Final Performance Summary ==========");
		System.out.println("input=" + input);
		System.out.println("rows=" + table.rows + ", cols=" + table.cols + ", numKeys=" + numKeys
				+ ", colBits=" + colBits + ", dataType=" + dataType);
		System.out.println("sort_time_s=" + String.format("%.6f", sortSeconds[0]));
		System.out.println("wall_time_s=" + String.format("%.6f", wallSeconds));
		if (commCost[0] >= 0) {
			System.out.println("communication_MB=" + String.format("%.6f", commMB));
			System.out.println("communication_bytes=" + commCost[0]);
		} else {
			System.out.println("communication_MB=measurement_failed");
		}
		System.out.println("peak_memory_MB=" + String.format("%.6f", peakMemoryMB));
		System.out.println("RESULT_CSV," + input + "," + table.rows + ","
				+ table.cols + "," + numKeys + "," + colBits + "," + dataType + ","
				+ sortSeconds[0] + "," + wallSeconds + "," + commMB + "," + peakMemoryMB);
		System.out.println("================================================\n");
	}

	private void setThreadedIO(Object network, boolean value) {
		try {
			java.lang.reflect.Field field = network.getClass().getSuperclass().getDeclaredField("THREADEDIO");
			field.setAccessible(true);
			field.setBoolean(network, value);
		} catch (Exception e) {
			e.printStackTrace();
		}
	}

	private <T> void runSort(CompEnv<T> env, TableInput table, int numKeys, int colBits,
			boolean isAlice, long[] commResult, double[] sortSecondsResult) {
		BitonicSortLib<T> sorter = new BitonicSortLib<>(env);

		int totalKeyBits = numKeys * colBits;
		int totalDataBits = table.cols * colBits;

		boolean[][] plainKey = new boolean[table.rows][totalKeyBits];
		boolean[][] plainData = new boolean[table.rows][totalDataBits];

		for (int i = 0; i < table.rows; i++) {
			for (int col = 0; col < numKeys; col++) {
				writeBits(plainKey[i], (numKeys - 1 - col) * colBits, table.values[i][col], colBits);
			}
			for (int col = 0; col < table.cols; col++) {
				writeBits(plainData[i], col * colBits, table.values[i][col], colBits);
			}
		}

		T[][] key = isAlice ? env.inputOfAlice(plainKey) : env.inputOfAlice(blankLike(plainKey));
		T[][] data = isAlice ? env.inputOfAlice(plainData) : env.inputOfAlice(blankLike(plainData));

		T isAscending = env.newT(true);

		Flag.sw.flush();
		env.flush();
		long commBefore = (!isAlice && env.channel instanceof Client)
				? readClientCommunicationBytes((Client) env.channel)
				: -1L;
		Flag.sw.startTotal();
		long sortStart = System.nanoTime();

		sorter.sortWithPayload(key, data, isAscending);

		env.flush();

		double timeSeconds = (System.nanoTime() - sortStart) / 1000000000.0;
		Flag.sw.stopTotal();
		if (sortSecondsResult != null) {
			sortSecondsResult[0] = timeSeconds;
		}

		if (!isAlice && commResult != null && env.channel instanceof Client) {
			long commAfter = readClientCommunicationBytes((Client) env.channel);
			commResult[0] = (commBefore >= 0 && commAfter >= commBefore) ? (commAfter - commBefore) : commAfter;
		}

		// Output reconstruction and the complete-tuple check are outside the sort timer.
		List<String> expectedTuples = new ArrayList<>();
		List<String> actualTuples = new ArrayList<>();
		long[] previous = null;
		for (int i = 0; i < table.rows; i++) {
			boolean[] opened = env.outputToAlice(data[i]);
			if (isAlice) {
				long[] row = new long[table.cols];
				for (int col = 0; col < table.cols; col++) {
					long value = 0L;
					for (int bit = 0; bit < colBits; bit++) if (opened[col * colBits + bit]) value |= 1L << bit;
					row[col] = value;
				}
				if (previous != null) {
					for (int col = 0; col < numKeys; col++) {
						if (previous[col] < row[col]) break;
						if (previous[col] > row[col]) throw new IllegalStateException("Sort output is not ascending");
					}
				}
				previous = row;
				expectedTuples.add(Arrays.toString(table.values[i]));
				actualTuples.add(Arrays.toString(row));
			}
		}
		env.flush();
		if (isAlice) {
			Collections.sort(expectedTuples);
			Collections.sort(actualTuples);
			if (!expectedTuples.equals(actualTuples)) throw new IllegalStateException("Complete tuple multiset differs from input");
			System.out.println("SORT_TUPLES_VERIFIED rows=" + table.rows + " cols=" + table.cols);
		}

		System.out.println("\n========== Party " + (isAlice ? "Alice" : "Bob") + " Performance ==========");
		System.out.println("Computation Time: " + String.format("%.6f", timeSeconds) + " s");
		System.out.println("================================================\n");
	}

	private void writeBits(boolean[] dest, int offset, long value, int colBits) {
		for (int bit = 0; bit < colBits; bit++) {
			int shift = bit;
			dest[offset + bit] = ((value >>> shift) & 1L) == 1L;
		}
	}

	private boolean[][] blankLike(boolean[][] data) {
		return new boolean[data.length][data[0].length];
	}

	private long readClientCommunicationBytes(Client client) {
		try {
			client.os.flush();

			java.lang.reflect.Field cosField = Client.class.getDeclaredField("cos");
			cosField.setAccessible(true);
			org.apache.commons.io.output.CountingOutputStream cos =
					(org.apache.commons.io.output.CountingOutputStream) cosField.get(client);

			java.lang.reflect.Field cisField = Client.class.getDeclaredField("cis");
			cisField.setAccessible(true);
			org.apache.commons.io.input.CountingInputStream cis =
					(org.apache.commons.io.input.CountingInputStream) cisField.get(client);

			if (cos != null && cis != null) {
				return cos.getByteCount() + cis.getByteCount();
			}
		} catch (Exception e) {
			e.printStackTrace();
		}
		return -1L;
	}

	private static final class TableInput {
		final int rows;
		final int cols;
		final long[][] values;

		TableInput(long[][] values, int rows, int cols) {
			this.values = values;
			this.rows = rows;
			this.cols = cols;
		}

		static TableInput read(String path) throws Exception {
			File file = new File(path);
			if (!file.isFile()) {
				throw new IllegalArgumentException("Input file not found: " + path);
			}

			List<long[]> rows = new ArrayList<>();
			int expectedCols = -1;
			try (BufferedReader reader = new BufferedReader(new FileReader(file))) {
				String line;
				while ((line = reader.readLine()) != null) {
					line = line.trim();
					if (line.isEmpty()) {
						continue;
					}
					String[] tokens = line.split("\\s+");
					if (expectedCols == -1) {
						expectedCols = tokens.length;
					} else if (tokens.length != expectedCols) {
						throw new IllegalArgumentException("Input file has inconsistent column counts: " + path);
					}
					long[] row = new long[tokens.length];
					for (int i = 0; i < tokens.length; i++) {
						row[i] = parseNumericCell(tokens[i]);
					}
					rows.add(row);
				}
			}

			if (rows.isEmpty()) {
				throw new IllegalArgumentException("Input file contains no table data: " + path);
			}

			long[][] values = rows.toArray(new long[rows.size()][]);
			return new TableInput(values, values.length, expectedCols);
		}

		private static long parseNumericCell(String token) {
			if (token.indexOf('.') >= 0 || token.indexOf('e') >= 0 || token.indexOf('E') >= 0) {
				return (long) Double.parseDouble(token);
			}
			return Long.parseLong(token);
		}
	}

	private static final class MemoryPeakTracker {
		private final MemoryMXBean memoryBean = ManagementFactory.getMemoryMXBean();
		private volatile boolean running = false;
		private volatile long peakBytes = currentUsedBytes();
		private Thread sampler;

		void start() {
			running = true;
			sampler = new Thread(() -> {
				while (running) {
					record();
					try {
						Thread.sleep(10);
					} catch (InterruptedException e) {
						Thread.currentThread().interrupt();
						return;
					}
				}
			}, "smcql-memory-peak-tracker");
			sampler.setDaemon(true);
			sampler.start();
		}

		void stop() throws InterruptedException {
			running = false;
			if (sampler != null) {
				sampler.join();
			}
			record();
			long procPeak = readProcStatusKb("VmHWM:");
			if (procPeak > 0) {
				peakBytes = Math.max(peakBytes, procPeak * 1024L);
			}
		}

		long peakBytes() {
			return peakBytes;
		}

		private void record() {
			peakBytes = Math.max(peakBytes, currentUsedBytes());
		}

		private long currentUsedBytes() {
			MemoryUsage heap = memoryBean.getHeapMemoryUsage();
			MemoryUsage nonHeap = memoryBean.getNonHeapMemoryUsage();
			return Math.max(0L, heap.getUsed()) + Math.max(0L, nonHeap.getUsed());
		}

		private long readProcStatusKb(String key) {
			File status = new File("/proc/self/status");
			if (!status.isFile()) {
				return -1L;
			}
			try (BufferedReader reader = new BufferedReader(new FileReader(status))) {
				String line;
				while ((line = reader.readLine()) != null) {
					if (line.startsWith(key)) {
						String value = line.substring(key.length()).trim().split("\\s+")[0];
						return Long.parseLong(value);
					}
				}
			} catch (Exception ignored) {
				return -1L;
			}
			return -1L;
		}
	}
}
