#!/usr/bin/env python3
"""Require three native Timer1 records and verify all reconstructed tuples."""
import argparse,json,re
from pathlib import Path

def collect(folder,input_file):
    expected=sorted(tuple(map(int,line.split())) for line in input_file.read_text().splitlines())
    records=[]
    for party in range(3):
        log=folder/f'p{party}/party{party}.log'
        s=log.read_text(errors='replace')
        matches=re.findall(r'^Time1 = ([0-9.eE+-]+) seconds \(([0-9.eE+-]+) MB',s,re.M)
        assert len(matches)==1,(log,'missing or repeated native Timer1')
        elapsed,comm_mb=map(float,matches[0])
        rss=re.findall(r'^FES_SORT_WINDOW_RSS_KIB=([0-9]+)$',s,re.M)
        sent=re.findall(r'^FES_SORT_WINDOW_SENT_BYTES=([0-9]+)$',s,re.M)
        assert len(sent)==1,(log,'missing exact native window sent bytes')
        assert len(rss)==1 and elapsed>0 and int(rss[0])>0,(log,'invalid window metrics')
        assert not any(x in s.lower() for x in ['mac fail','mac check failed','bad_value','malicious check failed'])
        records.append({'party':party,'time_s':elapsed,'sent_mib':int(sent[0])/(1024*1024),'rss_mib':int(rss[0])/1024})
    s=(folder/'p0/party0.log').read_text(errors='replace')
    output=[tuple(map(int,row.split())) for row in re.findall(r'^SORT_ROW (.*)$',s,re.M)]
    assert len(output)==len(expected),'Reconstructed tuple cardinality differs from input'
    assert all(output[i-1][0]<=output[i][0] for i in range(1,len(output))),'Output keys are not sorted'
    assert sorted(output)==expected,'Complete reconstructed tuple multiset differs from input'
    return {'time_s':max(r['time_s'] for r in records),'communication_mib':sum(r['sent_mib'] for r in records),'peak_rss_mib':max(r['rss_mib'] for r in records),'full_tuple_correct':True,'parties':records}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--folder',type=Path,required=True);parser.add_argument('--input',type=Path,required=True);parser.add_argument('--system',required=True);parser.add_argument('--case',required=True)
    args=parser.parse_args();result=collect(args.folder,args.input)
    (args.folder/'verified-window.json').write_text(json.dumps(result,indent=2))
    print('RESULT_CSV_HEADER,system,case,total_time_s,total_comm_MB,peak_memory_MB')
    print(f"RESULT_CSV,{args.system},{args.case},{result['time_s']:.9f},{result['communication_mib']:.9f},{result['peak_rss_mib']:.9f}")

if __name__=='__main__':main()
