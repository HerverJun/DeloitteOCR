"""Combine actual Playwright screen recording with offline Chinese narration and captions."""
import json
import math
from pathlib import Path
import shutil
import subprocess
import wave

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'reports/tutorial'
REC = WORK / 'recording'
OUT = ROOT / 'deliverables/花卉三分类教学'


def ass_time(seconds):
    ticks = round(seconds * 100)
    return f'{ticks // 360000}:{ticks // 6000 % 60:02d}:{ticks // 100 % 60:02d}.{ticks % 100:02d}'


def srt_time(seconds):
    ticks = round(seconds * 1000)
    return f'{ticks // 3600000:02d}:{ticks // 60000 % 60:02d}:{ticks // 1000 % 60:02d},{ticks % 1000:03d}'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    narration = {c['id']: c for c in json.loads((WORK/'narration.json').read_text(encoding='utf-8'))}
    timeline = json.loads((REC/'timeline.json').read_text(encoding='utf-8'))
    report = json.loads((REC/'report.json').read_text(encoding='utf-8'))
    assert report['status'] == 'passed', report.get('error')
    video = Path((REC/'video-path.txt').read_text(encoding='utf-8'))
    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(video)], text=True))
    duration = float(info['format']['duration'])
    rate = 22050
    pcm = bytearray(math.ceil(duration*rate)*2)
    events, subtitles, chapters = [], [], []
    header = '''[Script Info]
ScriptType: v4.00+
PlayResX: 1440
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,29,&H00FFFFFF,&H00FFFFFF,&H001C2D32,&H001C2D32,0,0,0,0,100,100,0,0,1,0,0,2,65,65,34,1
Style: Chapter,Microsoft YaHei,24,&H00FFFFFF,&H00FFFFFF,&H001C2D32,&H001C2D32,-1,0,0,0,100,100,0,0,1,0,0,7,30,30,14,1
Style: Brand,Microsoft YaHei,20,&H0099CEBF,&H00FFFFFF,&H001C2D32,&H001C2D32,0,0,0,0,100,100,0,0,1,0,0,9,30,30,17,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events.append(f'Dialogue: 0,0:00:00.00,{ass_time(duration)},Brand,,0,0,0,,Prism  ·  实机操作教学')
    for phase in timeline:
        chapter = narration[phase['id']]
        begin, end = phase['start'], min(phase['end'],duration)
        title = chapter['title']
        chapters.append((begin,end,title))
        events.append(f'Dialogue: 0,{ass_time(begin)},{ass_time(end)},Chapter,,0,0,0,,{title}')
        position=begin+0.4
        for clip in chapter['clips']:
            with wave.open(clip['path']) as source:
                assert (source.getnchannels(),source.getsampwidth(),source.getframerate()) == (1,2,rate)
                audio=source.readframes(source.getnframes())
            sample_start=round(position*rate)*2
            assert sample_start+len(audio)<=len(pcm), 'Audio would be truncated'
            pcm[sample_start:sample_start+len(audio)]=audio
            text=clip['text']
            # Two balanced lines keep the large captions in the dedicated lower band.
            if len(text)>42:
                middle=len(text)//2
                breaks=[i+1 for i,c in enumerate(text) if c in '，。；、' and abs(i-middle)<12]
                split=min(breaks,key=lambda i:abs(i-middle)) if breaks else middle
                caption=text[:split]+r'\N'+text[split:]
            else:
                caption=text
            finish=position+clip['duration']
            events.append(f'Dialogue: 1,{ass_time(position)},{ass_time(finish)},Default,,0,0,0,,{caption}')
            subtitles.append(f'{len(subtitles)+1}\n{srt_time(position)} --> {srt_time(finish)}\n{caption.replace(chr(92)+"N", chr(10))}\n')
            position=finish+0.35
    (WORK/'captions.ass').write_text(header+'\n'.join(events)+'\n',encoding='utf-8-sig')
    (OUT/'中文字幕.srt').write_text('\n'.join(subtitles),encoding='utf-8-sig')
    with wave.open(str(WORK/'narration-track.wav'),'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm)
    metadata=';FFMETADATA1\ntitle=Prism 从零开始图像分类教学\nartist=Prism 本地教学\n'
    for begin,end,title in chapters:
        metadata+=f'[CHAPTER]\nTIMEBASE=1/1000\nSTART={round(begin*1000)}\nEND={round(end*1000)}\ntitle={title}\n'
    (WORK/'chapters.ffmeta').write_text(metadata,encoding='utf-8')
    output=OUT/'Prism_从零开始图像分类教学.mp4'
    command=['ffmpeg','-y','-i',str(video),'-i','narration-track.wav','-f','ffmetadata','-i','chapters.ffmeta',
        '-map','0:v:0','-map','1:a:0','-map_metadata','2','-map_chapters','2',
        '-vf','pad=1440:1080:0:60:color=0x182B30,ass=captions.ass',
        '-af','loudnorm=I=-16:TP=-1.5:LRA=11','-c:v','libx264','-preset','medium','-crf','20','-threads','4',
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-ar','48000','-movflags','+faststart',str(output)]
    print(f'Rendering {duration:.1f} seconds to {output}',flush=True)
    with (WORK/'render.log').open('w',encoding='utf-8') as log:
        subprocess.run(command,cwd=WORK,stdout=log,stderr=log,check=True)
    final=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_format','-show_streams','-show_chapters','-of','json',str(output)],text=True,encoding='utf-8'))
    (WORK/'video-verification.json').write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copy2(REC/'classification-results.csv',OUT/'分类结果.csv')
    shutil.copy2(REC/'classification-results.jsonl',OUT/'分类结果.jsonl')
    shutil.copy2(REC/'flower-model.zip',OUT/'花卉分类模型.zip')
    shutil.copy2(REC/'report.json',OUT/'全流程验收记录.json')
    print(json.dumps({'video':str(output),'duration':duration,'chapters':len(chapters),'bytes':output.stat().st_size},ensure_ascii=False))


if __name__ == '__main__':
    main()
