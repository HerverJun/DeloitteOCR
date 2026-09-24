"""Token/price estimates for frozen requests; this is not an API billing receipt."""
import json
import math
from pathlib import Path
import statistics
import tiktoken

ROOT=Path(__file__).resolve().parents[1]


def main():
    try:codec=tiktoken.encoding_for_model('gpt-5.6-luna');mapping=True
    except KeyError:codec=tiktoken.get_encoding('o200k_base');mapping=False
    records=[]
    for suffix in ('06','07','08'):
        run='luna-local-review-20260919-'+suffix;root=ROOT/'build'/run
        for item in json.loads((root/'prepared.json').read_text('utf-8')):
            if not item['snapshot']:continue
            case=root/'inbox'/item['case']
            wire=json.loads((case/'wire.json').read_text('utf-8'))
            prompt=(case/'PROMPT.txt').read_text('utf-8')
            text=prompt+'PAGE CONTEXT; no reliable independent table crop; abstain if unreadable'+'STRUCTURE_CANDIDATE_DATA_JSON\n'+json.dumps(wire,ensure_ascii=False,separators=(',',':'))
            body_tokens=len(codec.encode(text,disallowed_special=()))
            width,height=item['sent_size']
            patches=math.ceil(width/32)*math.ceil(height/32)
            assert patches<=2500 and max(width,height)<=1600
            image_tokens=math.ceil(patches*1.2)
            response=(case/'response.json').read_text('utf-8')
            response_tokens=len(codec.encode(response,disallowed_special=()))
            estimate=(body_tokens+image_tokens)*.2/1e6+response_tokens*1.2/1e6
            ceiling_output=(body_tokens+image_tokens)*.2/1e6+2048*1.2/1e6
            records.append({'run':run,'case':item['case'],'candidate_count':len(wire['candidates']),
                'text_tokens_estimate':body_tokens,'image_tokens_estimate':image_tokens,'visible_response_tokens_estimate':response_tokens,
                'usd_with_visible_output_only':estimate,'usd_if_output_uses_entire_2048_budget':ceiling_output})
    primary=[r for r in records if not r['run'].endswith('08')]
    result={'model':'gpt-5.6-luna','tokenizer':codec.name,'tokenizer_model_mapping_available':mapping,
        'pricing_usd_per_million':{'input':.2,'output':1.2},'image_patch_size':32,'image_multiplier':1.2,
        'sources':['https://developers.openai.com/api/docs/models/gpt-5.6-luna','https://developers.openai.com/api/docs/guides/images-vision'],
        'not_measured':['actual endpoint billing','actual reasoning tokens','gateway surcharge','model latency','small message framing overhead'],
        'interpretation':'local tokenizer estimate and documented image formula; visible output estimate excludes hidden reasoning. Full output-budget column models 2048 charged output tokens, not measured usage or guaranteed invoice ceiling.',
        'primary_requests':len(primary),'single_candidate_primary_requests':sum(r['candidate_count']==1 for r in primary),
        'usd_per_primary_request_visible_min':min(r['usd_with_visible_output_only'] for r in primary),
        'usd_per_primary_request_visible_max':max(r['usd_with_visible_output_only'] for r in primary),
        'usd_per_primary_request_full_output_min':min(r['usd_if_output_uses_entire_2048_budget'] for r in primary),
        'usd_per_primary_request_full_output_max':max(r['usd_if_output_uses_entire_2048_budget'] for r in primary),
        'usd_per_1000_primary_requests_full_output_mean':statistics.mean(r['usd_if_output_uses_entire_2048_budget'] for r in primary)*1000,
        'records':records}
    path=ROOT/'audit/luna-local-review-20260919-06/cost-estimates.json'
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='records'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
