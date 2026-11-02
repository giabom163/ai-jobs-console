"""Source-backed job details; preserve unknowns and field-level provenance."""
import json
import re
import hashlib

EXPERIENCE_YEAR_PATTERN = r'(?<!\d)(?:\d{1,2}|十[一二三四五六七八九]?|[一二两三四五六七八九])\s*(?:(?:[-~至到]\s*\d{1,2}\s*)?年(?:以上|以下|以内|经验|工作|相关|从业|研发|开发|算法)?)(?!\s*\d{1,2}\s*月)'


def labelled_fields(text):
    """Only literal source labels; do not infer a department from a job title."""
    labels={'department':'所属部门|部门名称|业务部门|部门|所属团队|招聘部门',
            'experience':'工作经验|经验要求|工作年限',
            'education':'学历要求|学历', 'salary_text':'薪资范围|薪酬范围|薪资待遇'}
    fields={}
    for field,pattern in labels.items():
        match=re.search(r'(?m)^[ \t]*(?:'+pattern+r')[ \t]*[:：][ \t]*([^\r\n]{2,160})[ \t]*$',text)
        if not match and field=='department':
            match=re.search(r'(?m)(?:^|[。；;])[ \t]*招聘部门[：:][ \t]*([^\r\n]{2,160})$',text)
        if not match:continue
        value=match[1].strip()
        if value in {'暂无','未知','未披露','--','不详'} or re.search(r'(?:学历|经验|薪资|地点)[：:]',value):continue
        fields[field]={'value':value,'basis':'stored_source_label','evidence':match[1],
                       'start':match.start(1),'end':match.end(1),
                       'body_sha256':hashlib.sha256(text.encode()).hexdigest()}
    return fields


def labelled_sections(text):
    """Extract only clearly titled source sections, preserving exact offsets."""
    headings = {'duties': r'岗位职责|工作职责|职位职责',
                'requirements': r'岗位要求|任职要求|任职资格|职位要求|招聘要求'}
    boundary = r'岗位职责|工作职责|职位职责|岗位要求|任职要求|任职资格|职位要求|招聘要求|岗位亮点|职位亮点|岗位福利|薪资福利|部门介绍'
    pattern = re.compile(r'(?m)^[ \t]*[【\[]?(' + boundary + r')[】\]]?[：:]?[ \t]*$')
    matches = list(pattern.finditer(text))
    fields = {}
    for i, match in enumerate(matches):
        field = next((key for key, value in headings.items() if re.fullmatch(value, match[1])), None)
        if not field or field in fields:
            continue
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        if not value:
            continue
        fields[field] = {'value': value, 'basis': 'stored_source_section', 'heading': match[1],
                         'evidence': text[start:end], 'start': start, 'end': end,
                         'body_sha256': hashlib.sha256(text.encode()).hexdigest()}
    return fields


def department_name(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ' / '.join(dict.fromkeys(x.get('name', '').strip() for x in value if isinstance(x, dict) and x.get('name')))
    return ''


def source_details(con, job_id=None):
    sql="SELECT s.job_id,s.raw_json,s.seen_at,j.external_id,j.title FROM job_snapshots s JOIN jobs j ON j.id=s.job_id WHERE j.source='official:meituan' AND s.raw_json IS NOT NULL"
    args=[]
    if job_id:
        sql+=' AND s.job_id=?';args.append(job_id)
    result={}
    for row in con.execute(sql+' ORDER BY s.id',args):
        try:
            data=json.loads(row['raw_json'])
        except (ValueError,TypeError):
            continue
        if data.get('kind')=='official_job_detail' and isinstance(data.get('payload'),dict):
            if str(data['payload'].get('jobUnionId'))!=row['external_id'] or data['payload'].get('name')!=row['title']:
                continue
            result[row['job_id']]={'data':data['payload'],'observed_at':row['seen_at'],'url':data.get('url'),'payload_sha256':data.get('payload_sha256')}
    return result


def enrich_details(job, source=None):
    result=dict(job);text=result.get('description') or '';data=(source or {}).get('data') or {}
    fields=labelled_fields(text)
    fields.update(labelled_sections(text))
    for field,key in [('department','department'),('department_intro','departmentIntro'),('experience','workYear'),('duties','jobDuty'),('requirements','jobRequirement'),('recruitment_project','projectName')]:
        value=department_name(data.get(key)) if field=='department' else data.get(key)
        if isinstance(value,str) and value.strip():
            fields[field]={'value':value.strip(),'basis':'source_field','field':key,'url':source.get('url'),'observed_at':source.get('observed_at')}
    if result.get('department') and not data.get('department') and (not fields.get('department') or fields['department']['value']!=result['department']):
        fields['department']={'value':result['department'],'basis':'stored_source_field'}
    # The UI screenshot and the matching native detail establish social type 3;
    # internship type 2 is independently explicit in the source job titles.
    if str(data.get('jobType'))=='3':
        result['employment_type']='regular'
        fields['recruitment_type']={'value':'社招-正式','basis':'source_code','field':'jobType=3','url':(source or {}).get('url')}
    elif str(data.get('jobType'))=='2' and '实习' in result.get('title',''):
        result['employment_type']='intern'
        fields['recruitment_type']={'value':'实习','basis':'source_code_and_title','field':'jobType=2'}
    summary=result.get('requirements_summary') or {}
    field_patterns={
        'education':r'本科|硕士|博士|研究生|学士|大专|专科|中专|高中|学历|bachelor|master|ph\.?d|degree',
        'experience':EXPERIENCE_YEAR_PATTERN+r'|\d{1,2}\s*(?:\+|plus)?\s*(?:years?|yrs?)|经验不限|应届|实习.*[月天]|(?:(?:项目|实习|行业|相关|研发|算法|产品|AI|大模型|开发)?(?:工作)?经验)(?:者)?(?:优先|最佳|要求|不限)?|(?:工作|实习|项目|行业)经历',
    }
    used_excerpts=set()
    for field,pattern in field_patterns.items():
        if field in fields:
            continue
        other_pattern=field_patterns['experience' if field=='education' else 'education']
        for item in summary.get('items',[]):
            source_text=str(item.get('text') or '')
            # Split only at source punctuation. If one clause still combines
            # education and experience, keep it unresolved instead of copying
            # the same full sentence into both fields.
            clauses=[x.strip(' \t\r\n-•·▪●') for x in re.split(r'[,，;；。\n]+',source_text) if x.strip(' \t\r\n-•·▪●')]
            def excerpt_for(clause):
                if len(clause)>180 or not re.search(pattern,clause,re.I):
                    return None
                if re.search(other_pattern,clause,re.I):
                    # A single source clause may state graduation timing and
                    # degree together. Keep each field as its own literal slice.
                    if field=='education':
                        match=re.search(r'(?:本科|硕士|博士|研究生|学士|大专|专科|中专|高中|学历|bachelor|master|ph\.?d|degree)(?:(?:及以上|以上|在读|学历|研究生|毕业生)|[/／、](?:本科|硕士|博士|研究生)){0,3}',clause,re.I)
                    else:
                        match=re.search(r'(?:'+EXPERIENCE_YEAR_PATTERN+r'[^\n,，。；;且并]{0,20}|\d{1,2}\s*(?:\+|plus)?\s*(?:years?|yrs?)[^\n,，。；;且并]{0,20}|经验不限|应届(?:[（(][^）)]{1,20}[）)])?|实习[^\n,，。；;且并]{0,12}|(?:(?:项目|实习|行业|相关|研发|算法|产品|AI|大模型|开发)?(?:工作)?经验)(?:者)?(?:优先|最佳|要求|不限)?|(?:工作|实习|项目|行业)经历(?:者)?(?:优先|最佳)?)',clause,re.I)
                    return match.group(0).strip() if match else None
                return clause
            excerpt=next((value for clause in clauses if (value:=excerpt_for(clause))
                          and value not in used_excerpts),None)
            if excerpt:
                fields[field]={'value':excerpt,'basis':'requirement_excerpt','priority':item.get('priority'),
                               'evidence':item.get('evidence'),'excerpt':excerpt,
                               'requirement_start':item.get('start'),'requirement_end':item.get('end')}
                used_excerpts.add(excerpt)
                break
    # Unlabelled historical descriptions stay intact and clearly marked.
    result['job_details']=fields
    result['full_description']=text
    result['detail_source_checked_at']=(source or {}).get('observed_at')
    if fields.get('department'):result['department']=fields['department']['value']
    return result


def duplicate_records(con, job_id=None):
    sql="SELECT id,title,source,url,duplicate_of FROM jobs WHERE status='merged' AND duplicate_of IS NOT NULL"
    args=[]
    if job_id:sql+=' AND duplicate_of=?';args=[job_id]
    groups={}
    for row in con.execute(sql,args):groups.setdefault(row['duplicate_of'],[]).append({k:row[k] for k in ['id','title','source','url']})
    return groups
