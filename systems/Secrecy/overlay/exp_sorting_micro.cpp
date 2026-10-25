#include <stdio.h>
#include <assert.h>
#include <sys/time.h>
#include <sys/resource.h>
#include <fstream>
#include <vector>
#include <algorithm>

#include "exp-utils.h"

#define SHARE_TAG 193
#define PRIVATE static

extern "C" void secrecy_reset_communication_bytes();
extern "C" unsigned long long secrecy_get_communication_bytes();

PRIVATE Data** allocate_plain_table(int rows, int columns) {
  int length = sizeof(Data*) * rows + sizeof(Data) * rows * columns;
  Data** contents = (Data**) malloc(length);
  assert(contents != NULL);
  Data* ptr = (Data*) (contents + rows);
  for (int i = 0; i < rows; i++) {
    contents[i] = ptr + columns * i;
  }
  return contents;
}

PRIVATE void read_table_from_file(Table* table, const char* path, int rows,
                                  int columns) {
  Data** contents = allocate_plain_table(rows, columns);
  std::ifstream input(path);
  if (!input.is_open()) {
    fprintf(stderr, "ERROR: failed to open input file: %s\n", path);
    MPI_Abort(MPI_COMM_WORLD, 1);
  }
  for (int i = 0; i < rows; i++) {
    for (int j = 0; j < columns; j++) {
      long double value;
      if (!(input >> value)) {
        fprintf(stderr,
                "ERROR: input file %s has fewer than %d x %d values\n",
                path, rows, columns);
        MPI_Abort(MPI_COMM_WORLD, 1);
      }
      contents[i][j] = (Data) value;
    }
  }
  table->ownerId = -1;
  table->numRows = rows;
  table->numCols = columns;
  table->content = contents;
}

PRIVATE double elapsed_seconds(const struct timeval& begin,
                               const struct timeval& end) {
  return (end.tv_sec - begin.tv_sec) + (end.tv_usec - begin.tv_usec) * 1e-6;
}

PRIVATE double peak_rss_mb() {
  struct rusage usage;
  if (getrusage(RUSAGE_SELF, &usage) != 0) {
    return -1.0;
  }
  return usage.ru_maxrss / 1024.0;
}

/**
 * Evaluates the performance of ORDER-BY (bitonic sort).
 **/

int main(int argc, char** argv) {

  if (argc < 5) {
    printf("\n\nUsage: %s <NUM_ROWS> <NUM_COLS> <NUM_ATTRIBUTES> <INPUT_FILE>\n\n", argv[0]);
    return -1;
  }

  // initialize communication
  init(argc, argv);

  const int ROWS = atol(argv[1]); // input size
  const int COLS = atoi(argv[2]); // number of columns
  const int ATT = atoi(argv[3]); // number of leading sorting attributes
  const char* input_path = argv[4];
  assert(ATT<=COLS);
  assert(ATT<4 && ATT>0);

  const int rank = get_rank();
  const int pred = get_pred();
  const int succ = get_succ();

  // The input tables per party
  BShareTable t1 = {-1, rank, ROWS, 2*COLS, 1};
  allocate_bool_shares_table(&t1);

  struct timeval total_begin, total_end, sort_begin, sort_end;
  gettimeofday(&total_begin, 0);

  if (rank == 0) { //P1
    // Initialize input data and shares
    Table r1;
    read_table_from_file(&r1, input_path, ROWS, COLS);

    // t1 Bshare tables for P2, P3 (local to P1)
    BShareTable t12 = {-1, 1, ROWS, 2*COLS, 1};
    allocate_bool_shares_table(&t12);
    BShareTable t13 = {-1, 2, ROWS, 2*COLS, 1};
    allocate_bool_shares_table(&t13);
    
    init_sharing();

    // Generate boolean shares for r1
    generate_bool_share_tables(&r1, &t1, &t12, &t13);

    //Send shares to P2
    MPI_Send(&(t12.content[0][0]), ROWS * 2 * COLS, MPI_LONG_LONG, 1, SHARE_TAG, MPI_COMM_WORLD);

    //Send shares to P3
    MPI_Send(&(t13.content[0][0]), ROWS * 2 * COLS, MPI_LONG_LONG, 2, SHARE_TAG, MPI_COMM_WORLD);

    // free temp tables
    free(r1.content);
    free(t12.content);
    free(t13.content);

  }
  else if (rank == 1) { //P2
    MPI_Recv(&(t1.content[0][0]), ROWS * 2 * COLS, MPI_LONG_LONG, 0, SHARE_TAG, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
  }
  else { //P3
    MPI_Recv(&(t1.content[0][0]), ROWS * 2 * COLS, MPI_LONG_LONG, 0, SHARE_TAG, MPI_COMM_WORLD, MPI_STATUS_IGNORE);
  }

  //exchange seeds
  exchange_rsz_seeds(succ, pred);

  std::vector<unsigned int> att_index(ATT);
  bool* asc = (bool*) malloc(sizeof(bool) * ATT);
  assert(asc != NULL);
  for (int i = 0; i < ATT; i++) {
    att_index[i] = 2 * i;
    asc[i] = true;
  }
   
  /* =======================================================
     1. Measure CMP-SWAP batched
  ======================================================== */
  MPI_Barrier(MPI_COMM_WORLD);
  secrecy_reset_communication_bytes();

  // start timer
  gettimeofday(&sort_begin, 0);

  bitonic_sort_batch(&t1, att_index.data(), ATT, asc, ROWS/2);
  free(asc);
  
  // stop timer
  gettimeofday(&sort_end, 0);
  gettimeofday(&total_end, 0);

  double sort_elapsed = elapsed_seconds(sort_begin, sort_end);
  double total_elapsed = elapsed_seconds(total_begin, total_end);
  double max_sort_elapsed = 0.0;
  double max_total_elapsed = 0.0;
  unsigned long long local_comm = secrecy_get_communication_bytes();
  unsigned long long max_comm = 0;
  unsigned long long sum_comm = 0;
  double local_peak = peak_rss_mb();
  double max_peak = 0.0;

  MPI_Reduce(&sort_elapsed, &max_sort_elapsed, 1, MPI_DOUBLE, MPI_MAX, 0,
             MPI_COMM_WORLD);
  MPI_Reduce(&total_elapsed, &max_total_elapsed, 1, MPI_DOUBLE, MPI_MAX, 0,
             MPI_COMM_WORLD);
  MPI_Reduce(&local_comm, &max_comm, 1, MPI_UNSIGNED_LONG_LONG, MPI_MAX, 0,
             MPI_COMM_WORLD);
  MPI_Reduce(&local_comm, &sum_comm, 1, MPI_UNSIGNED_LONG_LONG, MPI_SUM, 0,
             MPI_COMM_WORLD);
  MPI_Reduce(&local_peak, &max_peak, 1, MPI_DOUBLE, MPI_MAX, 0,
             MPI_COMM_WORLD);

  if (rank == 0) {
    double max_comm_mb = max_comm / (1024.0 * 1024.0);
    double sum_comm_mb = sum_comm / (1024.0 * 1024.0);
    printf("%d\tORDER-BY\t%d\t%.3f\n", ATT, ROWS, max_sort_elapsed);
    printf("RESULT_SORTING_MICRO_CSV_HEADER,rows,cols,keys,input,total_time_s,sort_time_s,end_to_end_time_s,comm_max_MB,comm_sum_MB,peak_memory_MB\n");
    printf("RESULT_SORTING_MICRO_CSV,%d,%d,%d,%s,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f\n",
           ROWS, COLS, ATT, input_path, max_sort_elapsed, max_sort_elapsed, max_total_elapsed,
           max_comm_mb, sum_comm_mb, max_peak);
    printf("Total Time: %.6f s\n", max_total_elapsed);
    printf("Sort Time: %.6f s\n", max_sort_elapsed);
    printf("Communication Volume (max party): %.6f MB\n", max_comm_mb);
    printf("Communication Volume (sum parties): %.6f MB\n", sum_comm_mb);
    printf("Peak RSS (max party): %.6f MB\n", max_peak);
  }

  // Reconstruct and verify the complete relation after all measured metrics.
  std::vector<BShare> local_output(ROWS * COLS);
  std::vector<Data> opened_output(ROWS * COLS);
  for (int i = 0; i < ROWS; ++i) for (int j = 0; j < COLS; ++j)
    local_output[i * COLS + j] = t1.content[i][2 * j];
  open_b_array(local_output.data(), ROWS * COLS, opened_output.data());
  int valid = 1;
  if (rank == 0) {
    Table original;
    read_table_from_file(&original, input_path, ROWS, COLS);
    std::vector<std::vector<Data>> expected(ROWS), actual(ROWS);
    for (int i = 0; i < ROWS; ++i) {
      expected[i].assign(original.content[i], original.content[i] + COLS);
      actual[i].assign(opened_output.begin() + i * COLS, opened_output.begin() + (i + 1) * COLS);
      if (i > 0 && std::lexicographical_compare(actual[i].begin(), actual[i].begin() + ATT,
                                               actual[i-1].begin(), actual[i-1].begin() + ATT)) valid = 0;
    }
    std::sort(expected.begin(), expected.end());
    std::sort(actual.begin(), actual.end());
    if (expected != actual) valid = 0;
    free(original.content);
    if (valid) printf("SORT_TUPLES_VERIFIED rows=%d cols=%d\n", ROWS, COLS);
  }
  MPI_Bcast(&valid, 1, MPI_INT, 0, MPI_COMM_WORLD);
  if (!valid) MPI_Abort(MPI_COMM_WORLD, 2);

  // tear down communication
  MPI_Finalize();
  return 0;
}
