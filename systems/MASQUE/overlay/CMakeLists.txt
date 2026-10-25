add_executable(test
    test.cpp
)
target_compile_options(test PRIVATE "-Wall" "-Wextra" "-O2")
target_link_libraries(test 
    PUBLIC circsmp
    PUBLIC ABY::aby
    PUBLIC ENCRYPTO_utils::encrypto_utils
)

add_executable(sortcomp
    sortcomp.cpp
)

target_compile_options(sortcomp PRIVATE "-Wall" "-Wextra" "-O2")
target_link_libraries(sortcomp 
    PUBLIC circsmp
    PUBLIC ABY::aby
    PUBLIC ENCRYPTO_utils::encrypto_utils
)

add_executable(test_sorting_micro
    test_sorting_micro.cpp
)

target_compile_options(test_sorting_micro PRIVATE "-Wall" "-Wextra" "-O2")
target_link_libraries(test_sorting_micro 
    PUBLIC circsmp
    PUBLIC ABY::aby
    PUBLIC ENCRYPTO_utils::encrypto_utils
)

set(EXECUTABLE_OUTPUT_PATH ${PROJECT_BINARY_DIR})
