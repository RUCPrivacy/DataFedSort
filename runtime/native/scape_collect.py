#!/usr/bin/env python3
"""Require all native sorting parties and the rank-zero full-tuple verifier."""
import argparse,json,re
from pathlib import Path

def main():
    p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,required=True);p.add_argument('--security',choices=['semi-honest','malicious'],required=True);p.add_argument('--system',required=True);p.add_argument('--case',required=True)
    args=p.parse_args();records=[]
    for rank in range(3):
        log=args.folder/f'p{rank}/party{rank}.log';text=log.read_text(errors='replace')
        rows=re.findall(r'^RESULT_CSV,mpc4j_lan,(.*)$',text,re.M)
        assert len(rows)==1,(log,'missing or repeated native sorting record')
        fields=rows[0].split(',');assert len(fields)==9 and int(fields[0])==rank
        elapsed,comm,rss=map(float,fields[-3:]);assert elapsed>0 and comm>=0 and rss>0
        marker=f'SORT_PARTY_COMPLETED,rank={rank},security={args.security},materials=REAL,checks=COMPLETED,full_tuple='+('VERIFIED' if rank==0 else 'rank0')
        assert marker in text,(log,'no verified native completion marker')
        assert 'MpcAbortException' not in text and 'Sorting party failed' not in text
        records.append({'rank':rank,'time_s':elapsed,'communication_mib':comm,'peak_rss_mib':rss,'rows':int(fields[1]),'columns':int(fields[2]),'keys':int(fields[3]),'comparison_bits':int(fields[4]),'algorithm_id':int(fields[5])})
    assert len({(r['rows'],r['columns'],r['keys'],r['comparison_bits'],r['algorithm_id']) for r in records})==1
    result={'time_s':max(r['time_s'] for r in records),'communication_mib':sum(r['communication_mib'] for r in records),'peak_rss_mib':max(r['peak_rss_mib'] for r in records),'full_tuple_correct':True,'security':args.security,'real_materials':True,'sort_checks_completed':True,'parties':records}
    (args.folder/'verified-window.json').write_text(json.dumps(result,indent=2))
    print('RESULT_CSV_HEADER,system,case,total_time_s,total_comm_MB,peak_memory_MB')
    print(f"RESULT_CSV,{args.system},{args.case},{result['time_s']:.9f},{result['communication_mib']:.9f},{result['peak_rss_mib']:.9f}")
if __name__=='__main__':main()
