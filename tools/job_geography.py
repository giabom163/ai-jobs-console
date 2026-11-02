"""Conservative location evidence helpers; employer nationality is not location."""
import re
import unicodedata

VERSION = 'mainland-location-20261031-v3'

CN_PLACES = tuple('北京 上海 天津 重庆 深圳 广州 杭州 南京 苏州 无锡 常州 南通 扬州 镇江 泰州 盐城 徐州 淮安 宿迁 连云港 宁波 温州 嘉兴 湖州 绍兴 金华 台州 丽水 舟山 衢州 成都 武汉 西安 长沙 合肥 郑州 济南 青岛 烟台 潍坊 淄博 临沂 济宁 泰安 厦门 福州 泉州 漳州 莆田 龙岩 三明 南平 宁德 南昌 九江 赣州 上饶 宜春 景德镇 昆明 大理 德宏 贵阳 遵义 六盘水 昆山 佛山 东莞 珠海 惠州 中山 江门 肇庆 清远 汕头 湛江 茂名 韶关 潮州 揭阳 阳江 南宁 柳州 桂林 北海 海口 三亚 三沙 儋州 西宁 兰州 银川 乌鲁木齐 呼和浩特 包头 鄂尔多斯 拉萨 日喀则 石家庄 保定 唐山 廊坊 邯郸 秦皇岛 太原 大同 运城 沈阳 大连 鞍山 长春 吉林 哈尔滨 大庆'.split())
CN_PLACES += tuple('洛阳 芜湖 张家口 宜昌 蚌埠 黄冈 吉安 抚州 铜陵 赤峰 辽阳 通辽 乌兰察布 巴彦淖尔 宣城 威海 河源 黄石 万宁 日照 钦州 贵港 绵阳 宜宾 聊城 梅州 襄阳 毕节 安顺 铜仁 新余 鹰潭 盘锦 承德 咸阳 齐齐哈尔 牡丹江 佳木斯 鸡西 鹤岗 双鸭山 伊春 七台河 黑河 绥化 乌海 沧州 安庆 亳州 池州 滁州 鄂州 阜阳 淮北 淮南 黄山 荆州 琼海 六安 漯河 宿州 咸宁 信阳 岳阳 云浮 玉林 梧州 贺州 河池 百色 德阳 南充 泸州 达州 乐山 内江 自贡 眉山 遂宁 广安 攀枝花 广元 延安 资阳 巴中 山南 林芝 昌都 那曲 来宾 开封 德州 防城港 滨州 汕尾 怀化 汉中 榆林 安康 渭南 宝鸡 酒泉 本溪 营口 锦州 马鞍山 南阳 萍乡 大理白族自治州 恩施土家族苗族自治州 伊犁哈萨克自治州 德宏傣族景颇族自治州 凉山彝族自治州 甘孜藏族自治州 阿坝藏族羌族自治州 阿里地区 陵水黎族自治县 黔南布依族苗族自治州 黔西南布依族苗族自治州 黔东南苗族侗族自治州'.split())
CN_OBSERVED_LOCATION_DETAILS = tuple('北京朝阳区中冶霄云中心b座1205 北京朝阳区朝来科技园东区-2号楼705室 深圳龙岗区华为坂田基地'.split())
CN_REGIONS = tuple('河北省 山西省 辽宁省 吉林省 黑龙江省 江苏省 浙江省 安徽省 福建省 江西省 山东省 河南省 湖北省 湖南省 广东省 海南省 四川省 贵州省 云南省 陕西省 甘肃省 青海省 内蒙古自治区 广西壮族自治区 西藏自治区 宁夏回族自治区 新疆维吾尔自治区'.split())
MAINLAND_TERMS = ('中国大陆', '中国内地', '大陆', '内地')
CN_ENGLISH = re.compile(r"\b(?:Beijing|Shanghai|Shenzhen|Guangzhou|Hangzhou|Nanjing|Suzhou|Chengdu|Wuhan|Xian|Xi'an|Tianjin|Chongqing)\b", re.I)
MAINLAND_ENGLISH = re.compile(r'\bmainland(?: china)?\b', re.I)
FOREIGN_PLACES = tuple('香港 澳门 澳門 台湾 台灣 臺灣 台湾省 台灣省 臺灣省 台北 台中 高雄 新竹 科威特城 科威特 美国 加拿大 英国 伦敦 法国 巴黎 德国 柏林 日本 东京 韩国 首尔 新加坡 澳大利亚 悉尼 墨尔本 新西兰 印度 以色列 阿联酋 迪拜 沙特 卡塔尔 土耳其 瑞士 瑞典 挪威 丹麦 芬兰 荷兰 比利时 西班牙 意大利 爱尔兰 巴西 墨西哥 印度尼西亚 泰国 越南 马来西亚 菲律宾 巴基斯坦 埃及 南非 海外 境外'.split())
FOREIGN_PLACES += tuple('利雅得 圣保罗 麦纳麦 多哈 巴西利亚'.split())
FOREIGN_ENGLISH = re.compile(r'\b(?:Hong Kong|Macau|Macao|Taiwan|Taipei|Kuwait(?: City)?|United States|USA|Canada|United Kingdom|UK|London|France|Paris|Germany|Berlin|Japan|Tokyo|South Korea|Singapore|Australia|Sydney|Melbourne|New Zealand|India|Israel|United Arab Emirates|Dubai|Saudi Arabia|Qatar|Turkey|Switzerland|Sweden|Norway|Denmark|Finland|Netherlands|Belgium|Spain|Italy|Ireland|Brazil|Mexico|Indonesia|Thailand|Vietnam|Malaysia|Philippines|Pakistan|Egypt|South Africa|overseas|abroad)\b', re.I)
LOCATION_SEPARATOR = re.compile(r'[/、,，;；|]+')
UNBOUNDED_LOCATION = re.compile(r'全球|全国|不限地点|多个地点|\b(?:worldwide|anywhere|global|apac)\b', re.I)
REMOTE = re.compile(r'远程|\bremote\b', re.I)
NEGATED_MAINLAND = re.compile(r'(?:非|不在|不含|除外)(?:中国大陆|中国内地|大陆|内地)|\b(?:outside|excluding)\s+(?:mainland(?: china)?|china)\b', re.I)


def _normalize(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(value or '')).strip()).casefold()


def _domestic_hits(value):
    value = _normalize(value)
    hits = []
    if any(term.casefold() in value for term in MAINLAND_TERMS) or MAINLAND_ENGLISH.search(value):
        hits.extend(term for term in MAINLAND_TERMS if term.casefold() in value)
        match = MAINLAND_ENGLISH.search(value)
        if match:
            hits.append(match.group(0))
    if value in CN_REGIONS:
        hits.append(value)
    # Remove country/province prefixes only at the beginning, then match a
    # complete city/province token or a city followed by an administrative area.
    value = re.sub(r'^(?:中国大陆|中国内地|中国)', '', value)
    for region in sorted(CN_REGIONS, key=len, reverse=True):
        value = re.sub(r'^' + re.escape(region), '', value)
    tokens = [part.strip() for part in re.split(r'[/、,，;；|+\s]+', value) if part.strip()]
    for token in tokens:
        if token in CN_REGIONS:
            hits.append(token)
        if token in CN_OBSERVED_LOCATION_DETAILS:
            hits.append(token)
            continue
        for place in CN_PLACES:
            suffix = r'(?:市)?'
            if re.fullmatch(re.escape(place) + suffix, token):
                hits.append(token)
    hits.extend(match.group(0) for match in CN_ENGLISH.finditer(value))
    return list(dict.fromkeys(hits))


def _foreign_hits(value):
    value = _normalize(value)
    hits = [place for place in FOREIGN_PLACES if place.casefold() in value]
    hits.extend(match.group(0) for match in FOREIGN_ENGLISH.finditer(value))
    return list(dict.fromkeys(hits))


def classify_location(location):
    """Return domestic, mixed, foreign, or unknown from explicit location evidence."""
    return location_evidence(location)['classification']


def location_evidence(location):
    """Return an auditable, versioned classification without modifying the raw value."""
    raw = str(location or '')
    value = _normalize(location)
    if not value:
        return dict(raw=raw, normalized=value, classification='unknown', version=VERSION,
                    components=[], reason='empty_location')
    if NEGATED_MAINLAND.search(value):
        return dict(raw=raw, normalized=value, classification='unknown', version=VERSION,
                    components=[], reason='explicitly_excludes_mainland')
    parts = [part.strip() for part in LOCATION_SEPARATOR.split(value) if part.strip()]
    components = [dict(value=part, domestic_hits=_domestic_hits(part), foreign_hits=_foreign_hits(part))
                  for part in parts]
    has_domestic = any(item['domestic_hits'] for item in components)
    has_foreign = any(item['foreign_hits'] for item in components)
    remote_without_mainland = bool(REMOTE.search(value)) and not (
        any(term.casefold() in value for term in MAINLAND_TERMS) or bool(MAINLAND_ENGLISH.search(value)))
    if has_domestic and has_foreign:
        classification, reason = 'mixed', 'mainland_and_foreign_evidence'
    elif UNBOUNDED_LOCATION.search(value) or remote_without_mainland:
        classification, reason = 'unknown', 'unbounded_or_unscoped_remote'
    elif has_foreign:
        classification = 'foreign' if all(item['foreign_hits'] for item in components) else 'unknown'
        reason = 'foreign_locations_only' if classification == 'foreign' else 'unresolved_location_component'
    elif all(item['domestic_hits'] for item in components):
        classification, reason = 'domestic', 'all_components_mainland'
    else:
        classification, reason = 'unknown', 'unresolved_location_component'
    return dict(raw=raw, normalized=value, classification=classification, version=VERSION,
                components=components, reason=reason)


def is_domestic_only(location):
    return int(classify_location(location) == 'domestic')


def is_mixed_location(location):
    return int(classify_location(location) == 'mixed')


def is_unknown_location(location):
    return int(classify_location(location) == 'unknown')
