#!/usr/bin/env python3
"""Add RSS sampling to native timer 1 without changing protocol/algorithm code."""
from pathlib import Path
import sys,shutil
root=Path(sys.argv[1]).resolve()
assert root.name=='MP-SPDZ' and root.is_dir()
p=root/'Processor/BaseMachine.cpp'
s=p.read_text()
header=Path(__file__).with_name('rss_window.h')
shutil.copyfile(header,root/'Processor/fes_rss_window.h')
if '#include "fes_rss_window.h"' not in s:
    assert s.count('#include "BaseMachine.h"')==1
    s=s.replace('#include "BaseMachine.h"','#include "BaseMachine.h"\n#include "fes_rss_window.h"')
    old='void BaseMachine::start(int n)\n{'
    assert s.count(old)==1
    s=s.replace(old,old+'\n  if (n == 1) fes_sort_window_rss.reset(new FESNativeWindowRSS());')
    old='  timer[n].stop(total_comm());'
    assert s.count(old)==1
    s=s.replace(old,old+'\n  if (n == 1) {\n    cout << "FES_SORT_WINDOW_RSS_KIB=" << fes_sort_window_rss->stop() << endl;\n    cout << "FES_SORT_WINDOW_SENT_BYTES=" << timer[n].bytes_sent() << endl;\n    fes_sort_window_rss.reset();\n  }')
    p.write_text(s)
else:
    assert 'FES_SORT_WINDOW_RSS_KIB=' in s
print('Native Timer1 RSS instrumentation installed; rebuild the protocol VM before running.')
