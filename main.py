# -*- coding: utf-8 -*-
"""
astrbot_plugin_openai_image —— 对接 OpenAI 图片生成/编辑 API 的 AstrBot 插件

功能特性：
- 供应商选择：OpenAI 官方 / 硅基流动 SiliconFlow / 自定义中转站（OpenAI 兼容）
- API 地址自动隐藏并补全 /v1：无论用户是否填写 /v1 及其后的内容，都会自动识别、
  剥离并重新补全（WebUI 中展示的地址不含 /v1）
- 获取模型与供应商配置相互独立：通过指令 /模型 获取供应商支持的图像模型列表
- 生成指令可自定义（配置项「生成指令名」）；发送生成指令后进入收集模式，
  持续收集图片与文字素材，发送「开始」生成、「继续」继续收集、「取消」退出
- /绘图 生成图片；消息中直接附带图片，或引用（回复）含图片的消息时，会收集图片与文字进行图片编辑
- /设置模型 指令可直接切换模型（仅管理员）
"""

import asyncio
import base64
import collections
import json
import os
import re
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At as CompAt
from astrbot.api.message_components import Image as CompImage
from astrbot.api.message_components import Plain, Reply
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_openai_image"

# 供应商预设：供应商 -> 默认 API 地址（不含 /v1，插件一律自动补全 /v1）
# 仅收录 OpenAI 兼容且带图片生成接口的服务；只做图片生成，不涉及对话
PROVIDER_PRESETS = {
    "OpenAI 官方": "https://api.openai.com",
    "硅基流动 SiliconFlow": "https://api.siliconflow.cn",
    "阿里云百炼（通义万相）": "https://dashscope.aliyuncs.com/compatible-mode",
    "腾讯混元": "https://api.hunyuan.cloud.tencent.com",
    "302.AI": "https://api.302.ai",
    "自定义中转站（OpenAI 兼容）": "",
}

# 模型列表中筛选"图像模型"的识别关键词（小写匹配）
IMAGE_MODEL_KEYWORDS = (
    "dall",
    "gpt-image",
    "image",
    "flux",
    "cogview",
    "wanx",
    "qwen-image",
    "stable-diffusion",
    "sd3",
    "sdxl",
    "kolors",
    "hunyuan",
    "janus",
    "seedream",
    "midjourney",
    "paint",
    "t2i",
)

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})

# 风格指令：关键词 -> 风格提示词（直接作为免 # 前缀的指令使用，如「手办化 [图片]」）
STYLE_COMMANDS = {
    "手办化": "生成角色的手办造型，偏向立体模型展示",
    "桌面手办": (
        "根据提供的照片创建逼真、高质量的商业手办图像。使用 1/7 比例的 PVC 手办，"
        "摆放在真实室内电脑桌上。手办站在透明圆形亚克力底座上，底座无文字。"
        "电脑屏幕内容为该手办的 ZBrush 建模过程。显示器旁放置一个 BANDAI 风格的包装盒，"
        "印有角色的平面 2D 插图（仅风格参考，不要使用真实品牌名称或标志）。"
        "桌上放置典型的手办制作工具（刷子、颜料、刻刀）。\n\n"
        "严格保留输入照片中主体的身份和面部特征；保持姿势和整体构图与原始照片一致。"
        "只允许真实摄影因素的变化，如光照方向/强度、微妙的相机角度或景深、材质高光/阴影、背景模糊"
        "——绝不允许场景语义、姿势或身份的变化。\n\n"
        "清晰渲染 PVC 材质，具有自然反射和表面细节。"
        "避免卡通/绘画/3D 渲染感，呈现摄影级真实感。图像中不要出现文字、水印或标志。"
        "输出高分辨率图像，主体面部清晰可见。如果输入缺乏细节，请合理补全与主体一致的缺失细节。"
    ),
    "手办化2": "生成另一种风格的手办造型，可能是细节或比例的不同",
    "手办化3": "生成不同版本的手办展示，更偏系列感",
    "手办化4": "生成手办化第四种风格，可能是更精致或特殊造型",
    "手办化5": "生成另一种改良版手办造型",
    "手办化6": "生成手办化的第六种衍生风格",
    "Q版化": "生成Q版（可爱简化比例）的角色形象",
    "痛屋化": "生成痛屋（贴满角色元素装饰的房间）场景",
    "痛屋化2": "生成改良版痛屋场景，更丰富或现代感",
    "痛车化": "生成痛车（贴有角色图案的车辆）造型",
    "cos化": "生成角色cosplay化的照片风格",
    "cos自拍": "生成角色自拍风格的cos照片",
    "孤独的我": "生成孤独、滑稽或小丑化的意境图",
    "第三视角": "生成第三人称视角场景，看起来像他人在看角色",
    "鬼图": "生成灵异鬼图风格照片，带恐怖氛围",
    "第一视角": "生成第一人称视角场景，沉浸感强",
    "贴纸化": "生成贴纸风格的小图，方便做表情或周边",
    "玉足": "生成角色玉足相关的画面或细节",
    "fumo化": "生成毛绒玩偶（fumo）风格角色",
    "cos相遇": "生成两位cos角色相遇的场景",
    "三视图": "生成角色三视图（正面、侧面、背面）",
    "穿搭拆解": "生成角色服装穿搭的详细拆解图",
    "拆解图": "生成模型拆解或零件展示图",
    "角色界面": "生成类似游戏中角色信息界面的画面",
    "角色设定": "生成角色设定图，包含全身、武器、细节等",
    "3D打印": "生成适合3D打印的模型预览图",
    "微型化": "生成微缩模型、小比例角色形象",
    "挂件化": "生成挂件、钥匙扣风格的角色造型",
    "姿势表": "生成角色姿势参考表，多种动作合集",
    "高清修复": "对画面进行高清化、细节修复",
    "人物转身": "生成人物转身动作的连续画面",
    "绘画四宫格": "生成四宫格绘画对比图或进度展示",
    "发型九宫格": "生成九种不同发型的对比图",
    "头像九宫格": "生成九个不同风格的头像合集",
    "表情九宫格": "生成角色九种不同表情合集",
    "多机位": "生成多机位拍摄的场景视角合集",
    "电影分镜": "生成电影风格的分镜图",
    "动漫分镜": "生成动漫风格的分镜图",
    "真人化": "生成角色的真人化形象（真实感较强）",
    "真人化2": "生成另一种风格的真人化形象",
    "半真人": "生成半写实半动漫的混合风格",
    "半融合": "生成角色与其他元素融合的半融合风格",
    # ---- 装扮类 ----
    "女仆装": "生成角色穿女仆装打扮",
    "兔女郎": "生成角色兔女郎装扮",
    "泳装化": "生成角色泳装风格",
    "旗袍化": "生成角色旗袍装扮，中式优雅",
    "汉服化": "生成角色汉服古风装扮",
    "和服化": "生成角色和服装扮",
    "洛丽塔化": "生成洛丽塔洋装风格",
    "圣诞装": "生成圣诞主题装扮",
    "偶像化": "生成偶像舞台演出风格",
    # ---- 风格化 ----
    "机甲化": "生成机甲战士风格，金属装甲覆盖",
    "机娘化": "生成机娘（机械少女）风格，机械部件与少女结合",
    "骑士化": "生成中世纪骑士盔甲风格",
    "朋克化": "生成赛博朋克风格，霓虹与科技感",
    "蒸汽朋克": "生成蒸汽朋克风格，黄铜齿轮机械感",
    "废土风": "生成废土末世生存风格",
    "武侠化": "生成武侠江湖风格",
    "修仙化": "生成仙侠修仙风格",
    "魔法少女": "生成魔法少女变身风格",
    # ---- 萌化 / 兽化 ----
    "猫娘化": "生成猫娘（猫耳少女）风格",
    "狐娘化": "生成狐娘风格",
    "兽耳化": "生成兽耳娘风格",
    "龙娘化": "生成龙娘风格",
    "恶魔化": "生成小恶魔风格",
    "天使化": "生成天使圣洁风格",
    "魅魔化": "生成魅魔风格",
    "吸血鬼化": "生成吸血鬼贵族风格",
    "精灵化": "生成精灵风格",
    "兽人化": "生成兽人（furry）风格",
    "史莱姆化": "生成史莱姆软胶质感",
    "幼年化": "生成幼年化可爱形象",
    "性别反转": "生成性别反转形象",
    "胖化": "生成圆润胖版形象",
    "肌肉化": "生成肌肉壮汉风格",
    "SD化": "生成SD二头身Q版风格",
    "大头化": "生成大头Q版风格",
    # ---- 画风 ----
    "漫画化": "生成日式漫画风格",
    "美漫化": "生成美式漫画风格",
    "赛璐璐": "生成赛璐璐上色动画风格",
    "厚涂化": "生成厚涂绘画风格",
    "素描化": "生成素描风格",
    "线稿化": "生成干净线稿风格",
    "油画化": "生成油画风格",
    "水彩化": "生成水彩画风格",
    "水墨化": "生成水墨国画风格",
    "像素化": "生成像素画（8-bit）风格",
    "波普化": "生成波普艺术（pop art）风格",
    "浮世绘": "生成浮世绘风格",
    "敦煌风": "生成敦煌壁画风格",
    "极简风": "生成极简设计风格",
    "扁平化": "生成扁平插画风格",
    # ---- 材质 / 实物 ----
    "乐高化": "生成乐高积木人风格",
    "黏土化": "生成黏土模型风格",
    "陶瓷化": "生成陶瓷手办风格",
    "剪纸化": "生成剪纸艺术风格",
    "折纸化": "生成折纸风格",
    "毛绒化": "生成毛绒玩具风格",
    "木雕化": "生成木雕风格",
    "铜像化": "生成铜像雕像风格",
    "石膏像": "生成石膏像风格",
    "玻璃化": "生成玻璃水晶材质风格",
    "糖果化": "生成糖果甜点材质风格",
    "果冻化": "生成果冻质感风格",
    "金属化": "生成金属质感雕塑风格",
    "徽章化": "生成徽章吧唧风格",
    "立牌化": "生成亚克力立牌风格",
    "扭蛋化": "生成扭蛋胶囊玩具风格",
    "白模化": "生成手办白模（未上色灰模）风格",
    "可动化": "生成可动关节手办风格",
    # ---- 周边 / 设计 ----
    "T恤化": "生成T恤印花设计风格",
    "抱枕化": "生成抱枕图案风格",
    "壁纸化": "生成手机壁纸竖版风格",
    "海报化": "生成宣传海报风格",
    "杂志封面": "生成杂志封面风格",
    "表情包化": "生成表情包风格",
    "梗图化": "生成网络梗图风格",
    "游戏卡牌": "生成卡牌插画风格",
    "立绘化": "生成游戏立绘风格",
    "邮票化": "生成邮票设计风格",
    "泡面盖": "生成泡面盖卡通形象风格",
    # ---- 摄影 / 影视 ----
    "拍立得": "生成拍立得照片风格",
    "复古胶片": "生成胶片摄影风格",
    "老照片": "生成泛黄老照片风格",
    "黑白照": "生成黑白摄影风格",
    "电影感": "生成电影质感画面",
    "纪录片": "生成纪录片质感风格",
    "鱼眼镜头": "生成鱼眼镜头畸变风格",
    "无人机视角": "生成航拍俯瞰视角",
    "霓虹化": "生成霓虹灯风格",
    "故障艺术": "生成故障艺术（glitch）风格",
    "全息化": "生成全息投影风格",
    # ---- 特效 ----
    "火焰化": "生成火焰燃烧特效",
    "冰冻化": "生成冰冻冰霜特效",
    "雷电化": "生成雷电特效",
    "发光化": "生成发光光效",
    "粒子化": "生成粒子消散特效",
    "液金化": "生成液态金属质感",
    "马赛克化": "生成马赛克艺术风格",
    "万花筒": "生成万花筒对称图案",
    "镜像化": "生成镜像对称画面",
    "水中倒影": "生成水面倒影画面",
    "克隆化": "生成多个相同角色同框",
    "双人同框": "生成两人同框画面",
    "动作分解": "生成动作分解连续帧",
    "微缩世界": "生成移轴微缩世界风格",
    # ---- 场景 ----
    "雪景化": "生成雪景场景",
    "樱花化": "生成樱花场景",
    "星空化": "生成星空银河场景",
    "极光化": "生成极光天空场景",
    "云海化": "生成云海场景",
    "日落化": "生成日落剪影场景",
    "雨夜化": "生成雨夜场景",
    "深海化": "生成深海场景",
    "太空化": "生成太空宇宙场景",
    "赛博城": "生成赛博朋克城市夜景",
    "日式街道": "生成日式街道场景",
    "城堡化": "生成城堡场景",
    "神社化": "生成神社场景",
    "教室化": "生成教室场景",
    "天台化": "生成学校天台场景",
    "咖啡馆": "生成咖啡馆场景",
    "花海化": "生成花海场景",
    "沙漠化": "生成沙漠场景",
    "森林化": "生成森林场景",
    "竹林化": "生成竹林场景",
    "瀑布化": "生成瀑布场景",
    "熔岩化": "生成岩浆熔岩场景",
}

# 风格指令分类（/风格列表 按类别分条发送；键必须与 STYLE_COMMANDS 一一对应）
STYLE_CATEGORIES: list[tuple[str, list[str]]] = [
    (
        "手办类",
        [
            "手办化", "桌面手办", "手办化2", "手办化3", "手办化4", "手办化5", "手办化6",
            "3D打印", "微型化", "挂件化", "白模化", "可动化", "扭蛋化",
            "陶瓷化", "金属化", "石膏像", "铜像化", "木雕化", "玻璃化", "液金化",
        ],
    ),
    (
        "装扮类",
        [
            "女仆装", "兔女郎", "泳装化", "旗袍化", "汉服化", "和服化", "洛丽塔化",
            "圣诞装", "偶像化", "穿搭拆解",
        ],
    ),
    (
        "萌化/兽化",
        [
            "Q版化", "SD化", "大头化", "幼年化", "胖化", "肌肉化", "性别反转",
            "猫娘化", "狐娘化", "兽耳化", "龙娘化", "恶魔化", "天使化", "魅魔化",
            "吸血鬼化", "精灵化", "兽人化", "史莱姆化",
        ],
    ),
    (
        "风格化",
        [
            "机甲化", "机娘化", "骑士化", "朋克化", "蒸汽朋克", "废土风", "武侠化",
            "修仙化", "魔法少女", "cos化", "cos自拍", "cos相遇",
            "真人化", "真人化2", "半真人", "半融合",
        ],
    ),
    (
        "画风",
        [
            "漫画化", "美漫化", "赛璐璐", "厚涂化", "素描化", "线稿化", "油画化",
            "水彩化", "水墨化", "像素化", "波普化", "浮世绘", "敦煌风", "极简风",
            "扁平化", "剪纸化", "折纸化", "马赛克化", "万花筒",
        ],
    ),
    (
        "材质/实物",
        [
            "乐高化", "黏土化", "毛绒化", "fumo化", "糖果化", "果冻化",
        ],
    ),
    (
        "周边/设计",
        [
            "贴纸化", "徽章化", "立牌化", "T恤化", "抱枕化", "壁纸化", "海报化",
            "杂志封面", "表情包化", "梗图化", "游戏卡牌", "立绘化", "邮票化", "泡面盖",
        ],
    ),
    (
        "摄影/影视",
        [
            "拍立得", "复古胶片", "老照片", "黑白照", "电影感", "纪录片", "鱼眼镜头",
            "无人机视角", "霓虹化", "故障艺术", "全息化", "第一视角", "第三视角",
            "多机位", "电影分镜", "动漫分镜", "高清修复",
        ],
    ),
    (
        "特效/趣味",
        [
            "火焰化", "冰冻化", "雷电化", "发光化", "粒子化", "镜像化", "水中倒影",
            "克隆化", "双人同框", "动作分解", "微缩世界", "孤独的我", "鬼图",
        ],
    ),
    (
        "场景类",
        [
            "痛屋化", "痛屋化2", "痛车化", "雪景化", "樱花化", "星空化", "极光化",
            "云海化", "日落化", "雨夜化", "深海化", "太空化", "赛博城", "日式街道",
            "城堡化", "神社化", "教室化", "天台化", "咖啡馆", "花海化", "沙漠化",
            "森林化", "竹林化", "瀑布化", "熔岩化",
        ],
    ),
    (
        "视图/参考",
        [
            "三视图", "拆解图", "角色界面", "角色设定", "姿势表", "人物转身",
            "绘画四宫格", "发型九宫格", "头像九宫格", "表情九宫格", "玉足",
        ],
    ),
]

# 分类一致性检查：分类表中的键必须与 STYLE_COMMANDS 完全一致（不匹配时告警，不阻断加载）
_cat_keys = [k for _, ks in STYLE_CATEGORIES for k in ks]
if sorted(_cat_keys) != sorted(STYLE_COMMANDS):
    missing = sorted(set(STYLE_COMMANDS) - set(_cat_keys))
    extra = sorted(set(_cat_keys) - set(STYLE_COMMANDS))
    logger.warning(f"风格分类与风格指令不一致：分类缺少 {missing}，分类多余 {extra}")
del _cat_keys

# 可选的图片尺寸
SIZE_OPTIONS = (
    "auto",
    "1024x1024",
    "1024x1792",
    "1792x1024",
    "1536x1024",
    "1024x1536",
    "512x512",
    "256x256",
)

REQUEST_TIMEOUT = 300.0  # 图片生成/编辑请求超时（秒）

COLLECT_IDLE_TIMEOUT = 1800.0  # 收集模式空闲超时（秒），超时自动退出

# 插件自身的全部指令名（收集模式下这些消息不会被当作素材）
OUR_COMMANDS = frozenset(
    {
        "绘图", "画画", "生成图片", "draw", "img",
        "模型", "获取模型", "刷新模型",
        "设置模型", "切换模型",
        "设置尺寸", "切换尺寸",
        "供应商", "站点", "切换供应商",
        "更新日志", "更新记录", "changelog",
        "风格列表", "样式列表",
        "群分析", "群聊报告", "分析报告", "群聊分析", "龙王榜",
        "白名单", "黑名单",
        "指令列表", "指令", "命令",
    }
) | frozenset(STYLE_COMMANDS)

# 群聊分析报告：话题提取时的停用词
GROUP_REPORT_STOPWORDS = frozenset(
    "的 了 是 我 你 他 她 它 们 这 那 就 都 也 很 在 有 和 与 及 或 不 没 吗 呢 吧 啊 呀 哦 嗯 哈 "
    "可以 什么 怎么 一个 一下 真的 没有 还是 但是 因为 所以 然后 现在 今天 明天 昨天 大家 我们 "
    "你们 他们 自己 这个 那个 这样 那样 时候 地方 东西 知道 觉得 谢谢 没事 不是 就是 反正 应该 "
    "可能 喜欢 开心 好玩 笑死 无语 卧槽 我去 我靠 哈哈 哈哈哈 牛逼 厉害 干嘛 咋 咋了 咋样 图片 表情 "
    "哈哈哈哈 哈哈哈哈哈 哦哦 嗯嗯 好的 好吧 算了 收到 在吗 怎么样 是不是 能不能 要不要 好不好 "
    "行不行 不知道 没关系 没问题 厉害了 绝了 救命 好耶 笑死了 打卡 签到 早上好 中午好 晚上好 "
    "晚安 早安 午安 前排 沙发 路过 支持 感谢 辛苦 加油".split()
)


@dataclass
class CollectSession:
    """一次收集模式的状态（按会话隔离，进程内存，重启后失效）。"""

    umo: str
    prompt: str  # 进入收集模式时的初始文字（指令参数 + 引用消息文字）
    images: list[list[str]]  # 收集到的图片候选引用列表（每张图片一组候选：本地路径/URL/文件名）
    texts: list[str]  # 收集到的文字段落
    owner_id: str  # 收集模式发起者用户 ID（仅发起者/主人可投递素材与控制）
    created_at: float
    last_active: float
    paused: bool = False  # 生成失败后暂停收集：不再收集普通消息，仅响应 开始/继续/取消
    generating: bool = False  # 生成中锁定：不收集任何消息
    sent_msg_ids: list = field(default_factory=list)  # 收集期间发送的状态消息 ID（用于自动撤回）

# 常见 API 错误码 -> 中文排查提示
API_ERROR_HINTS = {
    400: "请求参数有误：请检查模型名、尺寸、画质等配置（可用 /模型 查看可用模型）",
    401: "API Key 无效或未填写：请检查配置面板中的 Key",
    403: "API Key 无权限调用图片接口：请在供应商控制台检查 Key 的项目/功能限制",
    404: "接口或模型不存在：请检查 API 地址与模型名（可用 /模型 查看可用模型）",
    429: "触发限流或配额不足：账号余额/图片额度用尽，或当前套餐不含图片生成，请检查账号额度或联系中转站",
    500: "供应商服务端错误：请稍后重试",
}


class OpenAIImagePlugin(Star):
    """OpenAI 图片生成 / 编辑插件。

    指令：
    - 🖼️ /绘图 [提示词]（指令名可在配置中自定义）：进入收集模式，持续收集图片与文字素材，
      发送「开始」▶️ 生成、「继续」➕ 继续收集、「取消」❌ 退出；也可引用含图片的消息带入初始素材
    - 🔍 /模型：获取当前供应商支持的图像模型列表（与供应商配置相互独立）
    - ⚙️ /设置模型 <模型名>：切换图片模型（仅管理员/主人）
    - 📐 /设置尺寸 <尺寸>：切换图片尺寸（仅管理员/主人）
    - 📡 /供应商 <站点名称>：查看/添加/删除/切换/重命名中转站站点（仅管理员/主人）
    - ⚪ /白名单：添加/移除/查看白名单用户（@用户或填ID，仅管理员/主人）
    - ⚫ /黑名单：添加/移除/查看黑名单用户（@用户或填ID，仅管理员/主人）
    - 🎨 风格指令：手办化、Q版化、痛屋化、cos化、三视图、真人化 等 40+ 个（免 # 前缀，/风格列表 查看全部）
    - 📊 /群分析 [文字|图片] [小时数]：解析 AstrBot 日志生成群分析（龙王榜/群友称号/热门话题/总结）
    - 📜 /更新日志：查看插件更新日志
    - 📋 /指令列表：显示本插件的全部指令

    访问控制：配置面板可设置 主人 / 白名单 / 黑名单（用户 ID 可用 /sid 查看）。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._sem = None  # 并发信号量，懒加载
        # 收集模式状态：unified_msg_origin -> CollectSession
        self._collect: dict[str, CollectSession] = {}
        # 收集模式进入前的确认等待：unified_msg_origin -> (参数, 时间戳, 发起者)
        self._pending_collect: dict[str, tuple[str, float, str]] = {}
        # 风格菜单等待选择：unified_msg_origin -> (时间戳, 发起者)
        self._pending_style_menu: dict[str, tuple[float, str]] = {}
        # 生成任务限流：滑动窗口内任务启动时间戳
        self._task_times: collections.deque = collections.deque()
        self._rate_lock = asyncio.Lock()
        # 群聊消息记录（用于群分析，持久化到文件，重载/重启保留）
        self._group_msgs: dict[str, collections.deque] = {}
        self._records_loaded = False
        self._write_counters: dict[str, int] = {}
        self._group_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "group_msgs"
        )
        try:
            self._group_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"创建群消息存储目录失败: {e}")
        # 模型列表缓存（按供应商隔离，避免每次生成都请求 /v1/models）
        self._models_cache: list[str] = []
        self._models_cache_time: float = 0.0
        self._models_cache_provider: str = ""
        self._img_dir = (
            Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "images"
        )
        try:
            self._img_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"创建图片存储目录失败: {e}")
        # 启动时规范化 api_base：隐藏 /v1，只保留纯净的根地址
        self._normalize_base_config()
        # 将旧的单供应商配置迁移为站点列表
        self._ensure_stations()

    # ------------------------------------------------------------------
    # 配置工具
    # ------------------------------------------------------------------

    def _clean_base(self, raw: str) -> str:
        """剥离 /v1 及其之后的内容，返回纯净的 API 根地址。

        支持用户粘贴各种形态的地址：
        - https://api.openai.com/v1
        - https://api.openai.com/v1/images/generations
        - https://api.openai.com/images/generations
        - https://api.openai.com
        均会归一化为 https://api.openai.com
        """
        raw = (raw or "").strip().rstrip("/")
        # 去掉 /v1 及之后的所有内容（v1 以后的要隐藏）
        raw = re.sub(r"/v1(?:/.*)?$", "", raw)
        # 去掉形如 /images/xxx 的完整端点
        raw = re.sub(r"/images(?:/.*)?$", "", raw)
        return raw.rstrip("/")

    def _normalize_base_config(self) -> None:
        """将配置中的 api_base 与站点列表地址规范化为不含 /v1 的根地址并持久化。"""
        try:
            changed = False
            raw = str(self.config.get("api_base", "") or "").strip()
            cleaned = self._clean_base(raw)
            if raw != cleaned:
                self.config["api_base"] = cleaned
                changed = True
            stations = self.config.get("stations") or []
            for s in stations:
                if not isinstance(s, dict):
                    continue
                b = str(s.get("base", "") or "").strip()
                c = self._clean_base(b)
                if b != c:
                    s["base"] = c
                    changed = True
            if changed:
                self._save_config()
                logger.info("已自动规范化 API 地址（/v1 已隐藏，使用时自动补全）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"规范化 API 地址失败: {e}")

    def _ensure_stations(self) -> None:
        """将旧的单供应商配置（provider/api_base/api_key）迁移为站点列表（stations）。"""
        try:
            if self.config.get("stations"):
                return
            old_base = str(self.config.get("api_base", "") or "").strip()
            old_key = str(self.config.get("api_key", "") or "").strip()
            old_provider = str(self.config.get("provider", "") or "").strip()
            if not old_base and not old_key:
                return
            if not old_base:
                old_base = PROVIDER_PRESETS.get(old_provider, "") or ""
            name = old_provider or "默认站点"
            self.config["stations"] = [
                {
                    "__template_key": "station",
                    "name": name,
                    "base": self._clean_base(old_base),
                    "key": old_key,
                }
            ]
            self._save_config()
            logger.info(f"已迁移旧供应商配置为站点: {name}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"迁移站点配置失败: {e}")

    def _resolve_station(self) -> tuple[str, str]:
        """解析当前使用的站点，返回 (API 根地址不含 /v1, API Key)。

        优先使用站点列表（stations）中「当前站点」指定的站点，未指定/未匹配时用第一个；
        若「当前站点」名称已失效（站点被改名/删除），自动修正为第一个站点；
        站点列表为空时回退到旧的单供应商配置（含预设地址）。
        """
        stations = self.config.get("stations") or []
        if stations:
            active = str(self.config.get("active_station", "") or "").strip()
            chosen = None
            if active:
                for s in stations:
                    if isinstance(s, dict) and str(s.get("name", "") or "").strip() == active:
                        chosen = s
                        break
                if chosen is None and isinstance(stations[0], dict):
                    # 当前站点名称已失效（可能被改名/删除）：自动修正为第一个站点
                    first_name = str(stations[0].get("name", "") or "").strip()
                    self.config["active_station"] = first_name
                    self._save_config()
                    logger.info(
                        f"当前站点 {active!r} 不存在，已自动切换为 {first_name or '第一个站点'}"
                    )
            if chosen is None:
                chosen = stations[0] if isinstance(stations[0], dict) else {}
            base = self._clean_base(str(chosen.get("base", "") or ""))
            key = str(chosen.get("key", "") or "").strip()
            if base:
                return base, key
        # 回退：旧的单供应商配置
        provider = str(self.config.get("provider", "") or "").strip()
        base = self._clean_base(str(self.config.get("api_base", "") or ""))
        if not base:
            base = PROVIDER_PRESETS.get(provider, "") or ""
        key = str(self.config.get("api_key", "") or "").strip()
        return (base or "https://api.openai.com"), key

    def _active_station_name(self) -> str:
        """当前站点名称（用于展示）；名称失效时回退为第一个站点名。"""
        stations = self.config.get("stations") or []
        if stations and isinstance(stations[0], dict):
            active = str(self.config.get("active_station", "") or "").strip()
            if active:
                for s in stations:
                    if isinstance(s, dict) and str(s.get("name", "") or "").strip() == active:
                        return active
                return str(stations[0].get("name", "") or "")
            return str(stations[0].get("name", "") or "")
        return str(self.config.get("provider", "") or "")

    def _api_base(self) -> str:
        """获取完整 API 根地址（自动补全 /v1）。"""
        base, _ = self._resolve_station()
        return base + "/v1"

    def _api_key(self) -> str:
        _, key = self._resolve_station()
        return key

    def _headers(self) -> dict:
        headers = {"User-Agent": f"AstrBot-{PLUGIN_NAME}/1.0"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _model(self) -> str:
        return str(self.config.get("model", "") or "").strip()

    def _size(self, model: str) -> str:
        """根据模型约束修正尺寸配置。"""
        size = str(self.config.get("size", "") or "").strip()
        if size == "自动":
            size = "auto"
        if not size:
            return ""
        ml = model.lower()
        # auto 直接透传（所有模型均支持，由供应商/模型自行决定；仅 dall-e-2 因接口只接受固定尺寸而回退）
        if ml.startswith("dall-e-2") and size not in (
            "256x256",
            "512x512",
            "1024x1024",
        ):
            return "1024x1024"
        return size

    def _quality(self, model: str) -> str:
        """画质参数，仅 dall-e-3 支持 standard/hd。"""
        if model.lower().startswith("dall-e-3"):
            q = str(self.config.get("quality", "") or "").strip()
            return q if q in ("standard", "hd") else ""
        return ""

    def _n(self, model: str) -> int:
        """生成数量，仅 dall-e-2 支持 >1。"""
        try:
            n = int(self.config.get("n", 1) or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(n, 1)
        if n > 1 and not model.lower().startswith("dall-e-2"):
            n = 1
        return n

    def _save_config(self) -> None:
        """持久化当前配置。"""
        try:
            saver = getattr(self.config, "save_config_async", None)
            if saver:
                asyncio.create_task(saver())
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存配置失败: {e}")

    def _semaphore(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(2)  # 最多同时 2 个图片请求
        return self._sem

    # ------------------------------------------------------------------
    # 生成任务限流（滑动窗口）
    # ------------------------------------------------------------------

    def _rate_limit_params(self) -> tuple[int, int]:
        """返回 (窗口内最大任务数, 窗口秒数)。次数 <=0 表示不限流。"""
        try:
            count = int(self.config.get("rate_limit_count", 3) or 0)
        except (TypeError, ValueError):
            count = 3
        try:
            window = int(self.config.get("rate_limit_window", 60) or 60)
        except (TypeError, ValueError):
            window = 60
        return max(count, 0), max(window, 1)

    def _task_slot_wait(self) -> float:
        """预计需要等待的秒数（0 表示立即可开始）。"""
        count, window = self._rate_limit_params()
        if count <= 0:
            return 0.0
        now = time.time()
        while self._task_times and now - self._task_times[0] > window:
            self._task_times.popleft()
        if len(self._task_times) >= count:
            return max(0.0, self._task_times[0] + window - now)
        return 0.0

    async def _acquire_task_slot(self) -> None:
        """滑动窗口限流：窗口内最多 N 个生成任务，超出则等待窗口空闲。"""
        count, window = self._rate_limit_params()
        if count <= 0:
            return
        while True:
            async with self._rate_lock:
                now = time.time()
                while self._task_times and now - self._task_times[0] > window:
                    self._task_times.popleft()
                if len(self._task_times) < count:
                    self._task_times.append(now)
                    return
                wait_for = self._task_times[0] + window - now
            # 分段等待，避免长时间空等
            await asyncio.sleep(max(0.5, min(wait_for, 5)))

    # ------------------------------------------------------------------
    # 访问控制（主人 / 白名单 / 黑名单）
    # ------------------------------------------------------------------

    @staticmethod
    def _ids(config_value) -> list[str]:
        """规范化配置中的用户 ID 列表为字符串列表。"""
        value = config_value or []
        if not isinstance(value, list):
            value = [value]
        return [str(x).strip() for x in value if str(x).strip()]

    def _is_master(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "").strip()
        return bool(sender) and sender in self._ids(self.config.get("master_ids"))

    def _user_allowed(self, event: AstrMessageEvent) -> bool:
        """访问控制：主人 > 黑名单 > 白名单。

        - 主人：永远允许（不受黑名单/白名单限制）；
        - 黑名单：禁止（主人除外）；
        - 白名单非空：白名单用户 + 主人 可用；
        - 白名单为空但设置了主人：仅主人可用（主人相当于默认白名单）；
        - 主人与白名单均未设置：除黑名单外所有人都可用。
        拿不到用户 ID 时放行，避免误伤。
        """
        sender = str(event.get_sender_id() or "").strip()
        if not sender:
            return True
        if sender in self._ids(self.config.get("master_ids")):
            return True
        if sender in self._ids(self.config.get("blacklist_ids")):
            return False
        whitelist = self._ids(self.config.get("whitelist_ids"))
        if whitelist:
            return sender in whitelist
        if self._ids(self.config.get("master_ids")):
            # 设置了主人但无白名单：只有主人可用（其他用户需加入白名单）
            return False
        return True

    async def _reject_unauthorized(self, event: AstrMessageEvent) -> None:
        """无权限提示。"""
        await event.send(MessageChain([Plain("🚫 您没有使用本插件指令的权限。")]))

    # ------------------------------------------------------------------
    # 图片文件工具
    # ------------------------------------------------------------------

    def _new_image_path(self, suffix: str) -> Path:
        suffix = (suffix or "png").lower()
        if not suffix.startswith("."):
            suffix = "." + suffix
        if suffix not in IMAGE_EXTS:
            suffix = ".png"
        name = f"img_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}{suffix}"
        return self._img_dir / name

    def _suffix_from_url(self, url: str) -> str:
        path = url.split("?", 1)[0]
        suffix = os.path.splitext(path)[1].lower()
        return suffix if suffix in IMAGE_EXTS else "png"

    def _cleanup_images(self, max_keep: int = 200, max_age_days: int = 7) -> None:
        """清理过旧的生成图片：最多保留 max_keep 张，超过 max_age_days 天删除。"""
        try:
            files = sorted(
                self._img_dir.glob("img_*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            now = time.time()
            for f in files[max_keep:]:
                try:
                    f.unlink()
                except OSError:
                    pass
            for f in files:
                try:
                    if now - f.stat().st_mtime > max_age_days * 86400:
                        f.unlink()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass

    async def _resolve_image(self, event: AstrMessageEvent, ref: str) -> str | None:
        """将图片引用解析为本地文件路径。

        支持 http(s):// 链接、base64:// 数据、file:// URI、本地路径；
        OneBot/QQ 平台还会尝试调用平台的 get_image API 获取协议端缓存文件。
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        try:
            if ref.startswith("base64://"):
                data = base64.b64decode(ref[len("base64://"):])
                path = self._new_image_path("png")
                path.write_bytes(data)
                return str(path)
            if ref.startswith(("http://", "https://")):
                # 下载失败时自动降级尝试 OneBot get_image
                return await self._download_image(event, ref)
            if ref.startswith("file://"):
                local = unquote(urlparse(ref).path)
                # 兼容 Windows 的 file:///C:/... 形式（urlparse 会得到 /C:/...）
                for candidate in (local, local.lstrip("/\\")):
                    if candidate and os.path.exists(candidate):
                        return candidate
            if os.path.exists(ref):
                return ref
            # 裸文件名（如 OneBot 的 xxx.image）：搜索 AstrBot 数据目录下的常见缓存位置
            data_root = Path(get_astrbot_data_path())
            for base in (data_root, data_root / "temp"):
                p = base / ref
                if p.is_file():
                    return str(p)
            # OneBot/QQ：调用平台 get_image API 获取本地文件
            return await self._resolve_via_onebot(event, ref)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"解析图片失败 {ref[:100]}: {e}")
        return None

    async def _download_plain(self, url: str) -> str | None:
        """下载网络图片到本地，带浏览器 UA 与 Referer（QQ 图片 CDN 有时校验来源）。"""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://qun.qq.com/",
            }
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=True, headers=headers
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
            path = self._new_image_path(self._suffix_from_url(url))
            path.write_bytes(r.content)
            return str(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"下载图片失败 {url[:100]}: {e}")
        return None

    async def _download_image(self, event: AstrMessageEvent, url: str) -> str | None:
        """下载网络图片（重试 3 次），失败后尝试 OneBot get_image 兜底。"""
        for attempt in range(3):
            path = await self._download_plain(url)
            if path:
                return path
            await asyncio.sleep(1 + attempt)
        return await self._resolve_via_onebot(event, url)

    async def _resolve_via_onebot(self, event: AstrMessageEvent, ref: str) -> str | None:
        """OneBot/QQ 平台：调用 get_image API 获取图片的本地文件路径。"""
        try:
            from astrbot.core.utils.quoted_message.onebot_client import (  # noqa: PLC0415
                OneBotClient,
            )

            client = OneBotClient(event)
            data = await client.call(
                "get_image",
                {"file": ref},
                warn_on_all_failed=False,
                unwrap_data=True,
            )
            if not isinstance(data, dict):
                return None
            path = str(data.get("file") or "").strip()
            if path and os.path.exists(path):
                return path
            url = str(data.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                return await self._download_plain(url)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OneBot get_image 解析失败 {ref[:80]}: {e}")
        return None

    def _ensure_local_copy(self, path: str) -> str:
        """确保图片位于插件持久目录：复制临时文件，防止平台清理/URL 过期后失效。"""
        try:
            p = Path(path)
            if not p.is_file():
                return path
            if str(p.resolve()).startswith(str(self._img_dir.resolve())):
                return str(p)
            suffix = p.suffix.lower() if p.suffix.lower() in IMAGE_EXTS else "png"
            target = self._new_image_path(suffix)
            shutil.copy2(str(p), str(target))
            return str(target)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"复制图片到持久目录失败: {e}")
            return path

    async def _persist_collected_images(
        self, event: AstrMessageEvent, images: list[list[str]]
    ) -> None:
        """收集图片时立即解析并落盘到插件持久目录。

        解决「开始」时 QQ 图片 URL 过期 / 临时文件被清理导致无法获取的问题：
        把每张图片的第一个可用引用下载/复制为本地文件，并作为首选候选。
        """
        for candidates in images:
            for ref in candidates:
                local = await self._resolve_image(event, ref)
                if local:
                    local = self._ensure_local_copy(local)
                    if local and local not in candidates:
                        candidates.insert(0, local)
                    logger.info(f"收集模式：图片已落盘 {local}")
                    break
            else:
                logger.warning(f"收集模式：图片候选均无法解析 {candidates}，开始生成时可能失败")

    async def _prepare_edit_image(self, model: str, image_path: str) -> str:
        """dall-e-2 编辑接口要求 PNG 图片，尽量将其他格式转换为 PNG。"""
        if not model.lower().startswith("dall-e-2"):
            return image_path
        if image_path.lower().endswith(".png"):
            return image_path
        try:
            from PIL import Image as PILImage

            img = PILImage.open(image_path)
            new_path = self._new_image_path("png")
            img.convert("RGBA").save(new_path, "PNG")
            return str(new_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PIL 转换图片格式失败，将按原图发送: {e}")
            return image_path

    # ------------------------------------------------------------------
    # API 请求
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_error(e: Exception) -> str:
        """格式化异常信息，避免过长的报错刷屏。"""
        msg = str(e) or e.__class__.__name__
        if "upload media to cdn failed" in msg:
            msg += (
                "（微信个人号网关的 CDN 上传服务返回错误，多为网关服务端临时故障："
                "插件已自动重试；若持续失败请重启网关/重新扫码登录，或联系网关服务商）"
            )
        elif "failed to send" in msg:
            msg += (
                "（消息发送失败：多为平台网关上传失败、图片过大或格式不支持，"
                "请查看 AstrBot 日志中对应适配器的详细原因后重试）"
            )
        return msg[:600]

    def _normalize_image_for_send(self, path: str) -> str:
        """将待发送图片归一化为平台友好的格式/大小。

        微信个人号等平台对超大图片（>4MB）与特殊格式（WebP/GIF/BMP 等）支持不佳，
        发送前统一转码：格式异常或特殊格式转 PNG，超大图转 JPEG 压缩。
        """
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as img:
                fmt = (img.format or "").upper()
                ext = os.path.splitext(path)[1].lower().lstrip(".")
                ext_map = {
                    "jpg": "JPEG",
                    "jpeg": "JPEG",
                    "png": "PNG",
                    "webp": "WEBP",
                    "gif": "GIF",
                    "bmp": "BMP",
                }
                size = os.path.getsize(path)
                width, height = img.size
                needs_convert = (
                    ext_map.get(ext) != fmt
                    or fmt in ("WEBP", "GIF", "BMP", "TIFF")
                    or size > 4 * 1024 * 1024
                    or max(width, height) > 2048
                )
                if needs_convert:
                    if max(width, height) > 2048:
                        ratio = 2048 / max(width, height)
                        img = img.resize(
                            (int(width * ratio), int(height * ratio)),
                            PILImage.Resampling.LANCZOS,
                        )
                    new_path = self._new_image_path(
                        "png"
                        if img.mode == "RGBA" and size <= 4 * 1024 * 1024
                        else "jpg"
                    )
                    if new_path.suffix == ".png":
                        img.convert("RGBA").save(new_path, "PNG", optimize=True)
                    else:
                        img.convert("RGB").save(new_path, "JPEG", quality=88)
                    logger.info(
                        f"已为发送归一化图片: {path} -> {new_path} "
                        f"({size} -> {os.path.getsize(new_path)} 字节)"
                    )
                    return str(new_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"图片发送前归一化失败，按原图发送: {e}")
        return path

    def _api_error(self, r: httpx.Response) -> str:
        """从错误响应中提取可读的错误信息，并附带常见错误的中文排查提示。"""
        msg = ""
        try:
            j = r.json()
            err = j.get("error") or {}
            msg = err.get("message") or j.get("message") or ""
        except Exception:  # noqa: BLE001
            pass
        if not msg:
            return f"API 请求失败，状态码 {r.status_code}：{r.text[:300]}"
        hint = self._error_hint(r.status_code, msg)
        suffix = f"\n💡 提示：{hint}" if hint else ""
        return f"API 错误({r.status_code}): {msg}{suffix}"

    @staticmethod
    def _error_hint(status: int, msg: str) -> str:
        """根据状态码与错误内容返回针对性的中文排查提示。"""
        low = msg.lower()
        if "image generation quota" in low:
            return (
                "该错误来自 OpenAI 上游：图片生成额度与账号余额相互独立。"
                "OpenAI 官方账号请检查 Billing 充值及项目是否开通图片功能；"
                "中转站需单独开通「图片生成」额度/模型权限（余额≠图片额度），或联系客服。"
            )
        if "edit" in low and any(
            s in low
            for s in ("not support", "unsupported", "not found", "not exist", "no access")
        ):
            return (
                "当前供应商可能不支持图片编辑接口（/images/edits）：插件会自动回退为仅文字生成，"
                "或请更换支持图片编辑的供应商"
            )
        if "quota" in low or "billing" in low or "insufficient" in low:
            return "账号余额或配额不足，请充值或检查套餐额度"
        if "model" in low and (
            "not found" in low or "does not exist" in low or "not exist" in low
        ):
            return "模型不存在或未开通：请用 /模型 查看供应商实际可用的图像模型，再用 /设置模型 指定"
        if "permission" in low or "not allowed" in low or "access" in low:
            return "Key 无权限调用该接口/模型，请在供应商控制台检查权限"
        return API_ERROR_HINTS.get(status, "")

    async def _post_json_with_fallback(
        self, url: str, payload: dict
    ) -> dict:
        """发送 JSON 请求；若中继不支持 response_format=b64_json 则自动降级重试。"""
        async with self._semaphore():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                follow_redirects=True,
                headers=self._headers(),
            ) as client:
                r = await client.post(url, json=payload)
                if r.status_code != 200 and "response_format" in r.text:
                    payload.pop("response_format", None)
                    r = await client.post(url, json=payload)
            if r.status_code != 200:
                raise RuntimeError(self._api_error(r))
            return r.json()

    async def _post_multipart_with_fallback(
        self, url: str, fields: dict, file_field: tuple
    ) -> dict:
        """发送 multipart 请求（图片编辑）；不支持 b64_json 时自动降级重试。"""
        async with self._semaphore():
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(REQUEST_TIMEOUT),
                follow_redirects=True,
                headers=self._headers(),
            ) as client:
                r = await client.post(url, data=fields, files={"image": file_field})
                if r.status_code != 200 and "response_format" in r.text:
                    fields.pop("response_format", None)
                    r = await client.post(url, data=fields, files={"image": file_field})
            if r.status_code != 200:
                raise RuntimeError(self._api_error(r))
            return r.json()

    async def _save_result(self, item: dict) -> str:
        """将接口返回的图片（b64_json 或 url）保存为本地文件。"""
        b64 = item.get("b64_json")
        if b64:
            raw = base64.b64decode(b64)
            path = self._new_image_path("png")
            path.write_bytes(raw)
            self._cleanup_images()
            return str(path)
        url = item.get("url")
        if url:
            async with httpx.AsyncClient(
                timeout=120, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                r = await client.get(url)
                r.raise_for_status()
            path = self._new_image_path(self._suffix_from_url(url))
            path.write_bytes(r.content)
            self._cleanup_images()
            return str(path)
        raise RuntimeError("接口返回的数据中没有图片内容（b64_json / url）")

    async def _fetch_models(self) -> list[str]:
        """获取当前供应商支持的所有模型，并筛选出图像模型。"""
        url = f"{self._api_base()}/models"
        async with httpx.AsyncClient(
            timeout=60, follow_redirects=True, headers=self._headers()
        ) as client:
            r = await client.get(url)
        if r.status_code != 200:
            raise RuntimeError(self._api_error(r))
        data = r.json().get("data") or []
        ids = [
            str(m.get("id"))
            for m in data
            if isinstance(m, dict) and m.get("id")
        ]
        keywords = IMAGE_MODEL_KEYWORDS
        image_models = [m for m in ids if any(k in m.lower() for k in keywords)]
        if not image_models:
            # 接口正常但没匹配到关键词时，退化为全部列出，避免漏掉新模型
            image_models = ids
        return self._sort_image_models(list(set(image_models)))

    @staticmethod
    def _sort_image_models(models: list[str]) -> list[str]:
        """对图像模型排序：优先 gpt-image 系列、dall-e-3、dall-e-2，其余按名称排序。"""
        def rank(m: str) -> int:
            ml = m.lower()
            if "gpt-image" in ml:
                return 0
            if "dall-e-3" in ml:
                return 1
            if "dall-e-2" in ml:
                return 2
            return 3

        return sorted(models, key=lambda m: (rank(m), m.lower()))

    async def _get_image_models(self, force: bool = False) -> list[str]:
        """获取当前供应商的图像模型列表（带缓存，缓存按供应商隔离）。

        force 为 True 时强制重新拉取（用于 /模型 指令）。
        """
        now = time.time()
        provider = str(self.config.get("provider", "") or "").strip()
        if (
            not force
            and self._models_cache
            and self._models_cache_provider == provider
            and now - self._models_cache_time < 600
        ):
            return list(self._models_cache)
        models = await self._fetch_models()
        self._models_cache = models
        self._models_cache_time = now
        self._models_cache_provider = provider
        return list(models)

    async def _resolve_model(self) -> tuple[str, str]:
        """根据当前供应商解析可用的图像模型（不依赖默认模型）。

        - 配置了模型且在供应商列表中 -> 直接使用；
        - 配置为空或不在列表中 -> 自动从供应商列表中选择并保存；
        - 获取列表失败 -> 回退到配置值（可能为空），由调用方提示。
        返回 (模型名, 提示信息)。
        """
        configured = self._model()
        try:
            models = await self._get_image_models()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"自动获取模型列表失败，将使用配置的模型: {e}")
            return configured, ""
        if configured and configured in models:
            return configured, ""
        if not models:
            return configured, ""
        picked = models[0]
        self.config["model"] = picked
        self._save_config()
        if configured:
            note = f"配置的模型 {configured} 不在供应商列表中，已自动切换为 {picked}（可用 /设置模型 指定）"
        else:
            note = f"已自动根据供应商选择模型：{picked}"
        return picked, note

    # ------------------------------------------------------------------
    # 引用消息收集
    # ------------------------------------------------------------------

    async def _collect_quoted(self, event: AstrMessageEvent) -> tuple[str, list[str]]:
        """收集引用（回复）消息中的文字与图片。

        优先使用 AstrBot 官方引用消息解析器（支持 OneBot 远程拉取），
        失败或版本过旧时回退到手动解析 Reply 消息段。
        返回 (引用的文字, 引用的图片引用列表)。
        """
        text = ""
        image_refs: list[str] = []
        try:
            from astrbot.core.utils.quoted_message.extractor import (  # noqa: PLC0415
                extract_quoted_message_images,
                extract_quoted_message_text,
            )

            try:
                text = (await extract_quoted_message_text(event)) or ""
            except Exception as e:  # noqa: BLE001
                logger.debug(f"官方引用文字解析失败，将回退手动解析: {e}")
            try:
                image_refs = list(await extract_quoted_message_images(event)) or []
            except Exception as e:  # noqa: BLE001
                logger.debug(f"官方引用图片解析失败，将回退手动解析: {e}")
        except ImportError:
            logger.debug("当前 AstrBot 版本无官方引用消息解析器，使用手动解析")

        # 手动解析回退：扫描消息链中的 Reply 消息段，收集其 chain 里的文字与图片
        if not text or not image_refs:
            for comp in event.get_messages():
                if not (isinstance(comp, Reply) or getattr(comp, "type", None) == "reply"):
                    continue
                msg_str = getattr(comp, "message_str", None)
                if msg_str and msg_str not in text:
                    text = f"{text}\n{msg_str}".strip()
                for sub in getattr(comp, "chain", None) or []:
                    if not (
                        isinstance(sub, CompImage)
                        or getattr(sub, "type", None) == "image"
                    ):
                        continue
                    ref = sub.url or sub.file or ""
                    if ref and ref not in image_refs:
                        image_refs.append(ref)

        return text.strip(), list(dict.fromkeys(image_refs))

    def _collect_direct_images(self, event: AstrMessageEvent) -> list[list[str]]:
        """收集当前消息中直接附带的图片（非引用段）。

        返回每张图片的一组候选引用（本地路径 path / URL / file 字段），
        解析时逐个尝试，提高不同平台（尤其 OneBot/QQ）的兼容性。
        """
        refs: list[list[str]] = []
        for comp in event.get_messages():
            if not (
                isinstance(comp, CompImage) or getattr(comp, "type", None) == "image"
            ):
                continue
            candidates: list[str] = []
            # 优先使用适配器已下载好的本地路径
            path = str(getattr(comp, "path", "") or "").strip()
            if path and os.path.exists(path):
                candidates.append(path)
            url = str(comp.url or "").strip()
            if url:
                candidates.append(url)
            file_ = str(comp.file or "").strip()
            if file_:
                candidates.append(file_)
            candidates = [c for c in dict.fromkeys(candidates) if c]
            if candidates:
                self._merge_image_candidates(refs, [candidates])
        return refs

    @staticmethod
    def _merge_image_candidates(
        target: list[list[str]], new_groups: list[list[str]]
    ) -> None:
        """将新图片候选合并进列表：共享任一引用的视为同一张图片，合并候选并去重。

        解决同一张图片以不同引用形式（本地路径 / URL / 文件名，或引用消息与直接附图）
        被重复计数的问题。
        """
        for cand in new_groups:
            for existing in target:
                if set(cand) & set(existing):
                    for c in cand:
                        if c not in existing:
                            existing.append(c)
                    break
            else:
                target.append(list(cand))

    # ------------------------------------------------------------------
    # 生成 / 编辑
    # ------------------------------------------------------------------

    async def _generate(self, model: str, prompt: str, size_override: str | None = None) -> str:
        """文生图，返回本地图片路径。"""
        url = f"{self._api_base()}/images/generations"
        payload = {
            "model": model,
            "prompt": prompt,
            "n": self._n(model),
            "response_format": "b64_json",
        }
        size = size_override or self._size(model)
        if size:
            payload["size"] = size
        quality = self._quality(model)
        if quality:
            payload["quality"] = quality
        try:
            resp = await self._post_json_with_fallback(url, payload)
        except RuntimeError as e:
            raise RuntimeError(f"模型 {model}：{e}") from e
        data = resp.get("data") or []
        if not data:
            raise RuntimeError("接口未返回图片数据")
        return await self._save_result(data[0])

    async def _generate_edit(self, model: str, image_path: str, prompt: str) -> str:
        """图生图（编辑），返回本地图片路径。"""
        upload_path = await self._prepare_edit_image(model, image_path)
        url = f"{self._api_base()}/images/edits"
        fields = {
            "model": model,
            "prompt": prompt,
            "n": self._n(model),
            "response_format": "b64_json",
        }
        size = self._size(model)
        if size:
            fields["size"] = size
        mime = "image/png" if upload_path.lower().endswith(".png") else "image/jpeg"
        with open(upload_path, "rb") as f:
            file_data = f.read()
        file_field = (os.path.basename(upload_path), file_data, mime)
        try:
            resp = await self._post_multipart_with_fallback(url, fields, file_field)
        except RuntimeError as e:
            raise RuntimeError(f"模型 {model}：{e}") from e
        data = resp.get("data") or []
        if not data:
            raise RuntimeError("接口未返回图片数据")
        return await self._save_result(data[0])

    @staticmethod
    def _is_edit_unsupported_error(e: Exception) -> bool:
        """判断错误是否表明供应商不支持图片编辑接口（/images/edits）。"""
        msg = str(e)
        low = msg.lower()
        if "edit" in low and any(
            s in low
            for s in ("not support", "unsupported", "not found", "not exist", "no access")
        ):
            return True
        m = re.search(r"API 错误\((\d+)\)", msg)
        if m and int(m.group(1)) in (404, 405, 501):
            return True
        return False

    async def _generate_with_fallback(
        self,
        candidates: list[str],
        prompt: str,
        edit_image_path: str | None,
        size_override: str | None = None,
    ) -> tuple[str, str]:
        """按候选模型依次生成；遇到「图片生成额度不可用(429)」时自动换下一个模型重试。

        返回 (本地图片路径, 实际使用的模型名)。
        """
        tried: list[str] = []
        last_err: Exception | None = None
        for candidate in candidates:
            tried.append(candidate)
            try:
                if edit_image_path:
                    out = await self._generate_edit(candidate, edit_image_path, prompt)
                else:
                    out = await self._generate(candidate, prompt, size_override)
                return out, candidate
            except RuntimeError as e:
                last_err = e
                if "image generation quota" not in str(e).lower():
                    # 非图片额度类错误，直接抛出，不盲目换模型
                    raise
                logger.warning(f"模型 {candidate} 图片额度不可用，尝试下一个模型: {e}")
        raise RuntimeError(
            f"以下模型均无图片生成额度（429）：{'、'.join(tried)}。"
            "这是上游账号的图片额度/权限问题（余额≠图片额度），"
            "请检查或联系供应商开通图片生成权限。"
            f"最后错误：{last_err}"
        ) from last_err

    async def _send_image(self, event: AstrMessageEvent, path: str, caption: str) -> None:
        """发送图片（单独发送并自动重试），随后发送说明文字。

        部分平台网关（如微信个人号 weixin_oc）的 CDN 上传服务偶发 500，
        重试可显著提高成功率；图片与文字分开发送，重试时不会重复文字。
        """
        send_path = self._normalize_image_for_send(path)
        image_chain = MessageChain([CompImage.fromFileSystem(send_path)])
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                await event.send(image_chain)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(f"图片发送失败（第 {attempt + 1} 次），稍后重试: {e}")
                if attempt < 2:
                    await asyncio.sleep(2 + attempt)
        else:
            raise RuntimeError(f"图片发送失败：{self._fmt_error(last_err)}") from last_err
        if caption:
            await event.send(MessageChain([Plain(caption)]))

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("绘图", alias={"画画", "生成图片", "draw", "img"})
    async def draw(self, event: AstrMessageEvent, prompt: GreedyStr):
        """🖼️ 进入图片收集模式：发送素材（图片/文字）后，发送「开始」▶️ 生成，「继续」➕ 继续收集，「取消」❌ 退出。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        try:
            await self._enter_collection(event, str(prompt).strip())
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            await event.send(MessageChain([Plain(f"😵 出错了：{self._fmt_error(e)}")]))

    @filter.command("指令列表", alias={"指令", "命令"})
    async def cmd_list(self, event: AstrMessageEvent):
        """📋 显示本插件的全部指令列表。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        cmd = str(self.config.get("draw_command", "") or "").strip() or "绘图"
        lines = [
            "📋 OpenAI 图片生成插件指令列表：",
            "",
            f"🖼️ /{cmd} [提示词] —— 进入收集模式",
            "　别名：/画画、/生成图片、/draw、/img",
            f"🖼️ /{cmd} + 消息中直接附图 / 引用含图片的消息 —— 附带素材作为初始素材",
            "▶️ 「开始」 —— 用已收集的素材开始生成",
            "➕ 「继续」 —— 继续收集素材",
            "❌ 「取消」 —— 退出收集模式，清空素材",
            "🔍 /模型 —— 获取当前供应商的图像模型列表",
            "　别名：/获取模型、/刷新模型",
            "⚙️ /设置模型 <模型名> —— 切换图片模型（仅管理员/主人）",
            "　别名：/切换模型",
            "📐 /设置尺寸 <尺寸> —— 切换图片尺寸（仅管理员/主人）",
            "　别名：/切换尺寸",
            "📡 /供应商 —— 查看/添加/删除/切换/重命名中转站站点（仅管理员/主人）",
            "　别名：/站点、/切换供应商",
            "⚪ /白名单 —— 添加/移除/查看白名单（@用户或填ID）",
            "⚫ /黑名单 —— 添加/移除/查看黑名单（@用户或填ID）",
            "🎨 风格指令 —— 手办化、Q版化、痛屋化、cos化、三视图、真人化 等 40+ 个",
            "　直接发送风格词+图片即可（免 # 前缀）；/风格列表 查看全部",
            "📊 /群分析 —— 解析 AstrBot 日志生成群分析（龙王榜/称号/话题/总结，文字或长图模式）",
            "📜 /更新日志 —— 查看插件更新日志",
            "",
            "💡 生成指令名可在配置面板「生成指令名」中自定义；",
            "　群聊中直接发送指令/素材即可，无需 / 前缀或 @ 机器人。",
            "🔒 主人 / 白名单 / 黑名单可在配置面板设置（用户 ID 可用 /sid 查看）。",
        ]
        await event.send(MessageChain([Plain("\n".join(lines))]))

    @filter.command("更新日志", alias={"更新记录", "changelog"})
    async def changelog(self, event: AstrMessageEvent):
        """📜 查看插件的更新日志（从开发初期开始的完整历史）。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        try:
            path = Path(__file__).resolve().parent / "CHANGELOG.md"
            if not path.is_file():
                await event.send(MessageChain([Plain("⚠️ 未找到更新日志文件（CHANGELOG.md）。")]))
                return
            content = path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            await event.send(MessageChain([Plain(f"😵 读取更新日志失败：{self._fmt_error(e)}")]))
            return
        # 展示全部版本（从开发初期开始），按版本号从新到旧排列；超长截断并提示
        sections = re.split(r"(?m)^(?=## )", content)
        shown = [sec.strip() for sec in sections if sec.startswith("## v")]
        text = "\n".join(shown)
        if len(text) > 4000:
            text = text[:4000] + "\n…（消息过长截断，完整日志见插件目录 CHANGELOG.md）"
        if not text.strip():
            text = "（更新日志为空）"
        await event.send(MessageChain([Plain(text.strip())]))

    @filter.command("风格列表", alias={"样式列表"})
    async def style_list(self, event: AstrMessageEvent):
        """🎨 风格指令菜单：先发分类菜单，回复数字或分类名查看对应分类。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        menu_lines = [
            f"🎨 风格指令菜单（共 {len(STYLE_COMMANDS)} 个 · {len(STYLE_CATEGORIES)} 类，直接发送即可，无需 # 前缀）",
            "回复「数字」或「分类名」查看对应分类：",
            "",
        ]
        menu_lines += [
            f"{i}. {name}（{len(keys)} 个）" for i, (name, keys) in enumerate(STYLE_CATEGORIES, 1)
        ]
        menu_lines += [
            "",
            "💡 如「5」或「画风」；发送「取消」退出菜单。",
        ]
        await event.send(MessageChain([Plain("\n".join(menu_lines))]))
        # 记录菜单等待状态（120 秒有效）
        self._pending_style_menu[event.unified_msg_origin] = (
            time.time(),
            str(event.get_sender_id() or "").strip(),
        )

    # ------------------------------------------------------------------
    # 群聊分析报告
    # ------------------------------------------------------------------

    def _avatar_url(self, event: AstrMessageEvent, uid: str) -> str:
        """构造用户头像 URL（QQ 平台用 qlogo；其他平台返回空，不带头像）。"""
        try:
            if event.get_platform_name() == "aiocqhttp" and uid.isdigit():
                return f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=100"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _extract_topics(self, texts: list[str], top: int = 6) -> list[str]:
        """从消息文本中提取热门话题关键词。

        优先 jieba 分词（依赖已加入 requirements.txt）；未安装时回退按标点切词。
        统一先剔除 URL/@提及/表情/数字，过滤停用词与单字；同一消息内重复词只计一次，
        避免刷屏把无意义词顶成"热门话题"。
        """
        cleaned: list[str] = []
        for t in texts:
            if not t:
                continue
            t = re.sub(r"https?://\S+", " ", t)
            t = re.sub(r"@\S+", " ", t)
            t = re.sub(r"[\U0001F000-\U0001FAFF\u2190-\u2BFF\uFE0F\u200D]", " ", t)
            t = re.sub(r"[a-zA-Z0-9]+", " ", t)
            cleaned.append(t)
        words: list[str] = []
        try:
            import jieba  # noqa: PLC0415

            for t in cleaned:
                seen: set[str] = set()
                for w in jieba.lcut(t):
                    if len(w) >= 2 and w not in GROUP_REPORT_STOPWORDS and w not in seen:
                        seen.add(w)
                        words.append(w)
        except Exception:  # noqa: BLE001
            for t in cleaned:
                for seg in re.split(
                    r"[，。！？、；：,.!?;: \t\r\n\"'“”‘’（）()【】\[\]]+", t
                ):
                    seg = seg.strip()
                    if 2 <= len(seg) <= 8 and seg not in GROUP_REPORT_STOPWORDS:
                        words.append(seg)
        counter: dict[str, int] = {}
        for w in words:
            counter[w] = counter.get(w, 0) + 1
        ranked = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)
        return [w for w, _ in ranked[:top]]

    @staticmethod
    def _assign_titles(users: dict[str, dict]) -> list[tuple[str, str]]:
        """按活跃特征分配群友称号，返回 [(uid, 称号文本)]。"""
        items = list(users.values())
        if not items:
            return []
        titles: list[tuple[str, str]] = []
        used: set[int] = set()

        def take(key, label, threshold):
            if not items:
                return
            chosen = max(items, key=key)
            if id(chosen) not in used and key(chosen) >= threshold:
                used.add(id(chosen))
                titles.append((chosen["uid"], label(chosen)))
            return chosen

        take(lambda u: u["count"], lambda u: f"👑 龙王 —— {u['name']}（{u['count']} 条）", 1)
        take(lambda u: u["late"], lambda u: f"🌙 夜猫子 —— {u['name']}（凌晨发言 {u['late']} 条）", 3)
        take(lambda u: u["imgs"], lambda u: f"🖼️ 图王 —— {u['name']}（发图 {u['imgs']} 张）", 3)
        take(lambda u: u["chars"], lambda u: f"✍️ 文豪 —— {u['name']}（共 {u['chars']} 字）", 50)
        # 话痨：消息第二多的人
        ranked = sorted(items, key=lambda u: u["count"], reverse=True)
        for u in ranked:
            if id(u) not in used:
                used.add(id(u))
                titles.append((u["uid"], f"💬 话痨 —— {u['name']}（{u['count']} 条）"))
                break
        return titles[:5]

    @filter.command("群分析", alias={"群聊报告", "分析报告", "群聊分析", "龙王榜"})
    async def group_report(self, event: AstrMessageEvent, hours_arg: GreedyStr):
        """📊 群分析：解析 AstrBot 日志生成龙王榜/群友称号/热门话题/总结。支持文字与图片（长图）模式，如「群分析 图片 48」。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        if not self.config.get("report_enabled", True):
            await event.send(
                MessageChain(
                    [Plain("⚠️ 群分析功能已关闭（可在配置面板「群聊分析开关」中开启）。")]
                )
            )
            return
        # 解析参数：模式（文字/图片）+ 小时数；「补录」子指令：从数据库补录更早的历史
        raw = str(hours_arg).strip()
        if raw.startswith("补录") or raw.startswith("backfill"):
            await self._backfill_history(event)
            return
        mode = str(self.config.get("report_mode", "文字") or "文字")
        hours = 24
        for token in raw.replace("，", " ").replace(",", " ").split():
            if token in ("文字", "文本"):
                mode = "文字"
            elif token in ("图片", "长图", "海报"):
                mode = "图片"
            else:
                try:
                    hours = int(token)
                except (TypeError, ValueError):
                    pass
        hours = min(max(hours, 1), 24 * 7)
        cutoff = time.time() - hours * 3600

        # 数据源：群消息事件记录（文档方式 GROUP_MESSAGE 监听），按 group_id 归属当前群；
        # 记录持久化到文件，重载/重启后旧聊天记录仍在
        self._ensure_records_loaded()
        group_key = str(event.message_obj.group_id or "") or event.unified_msg_origin
        msgs = self._group_msgs.get(group_key)
        if not msgs:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "⚠️ 当前群暂无消息记录（插件加载后开始统计并持久化保存）。"
                            "让群友聊一会再试。"
                        )
                    ]
                )
            )
            return
        recent = [m for m in msgs if m["ts"] >= cutoff]
        if not recent:
            await event.send(
                MessageChain([Plain(f"⚠️ 最近 {hours} 小时内没有消息记录。")])
            )
            return
        source_note = "群消息事件记录（持久化保存，重载/重启保留）"

        # 诊断日志：条数与发言用户分布
        try:
            uid_counts: dict[str, int] = {}
            for m in recent:
                uid_counts[m["uid"]] = uid_counts.get(m["uid"], 0) + 1
            logger.info(
                f"群分析：来源 [{source_note}]，共 {len(recent)} 条，"
                f"用户分布 {sorted(uid_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]}"
            )
        except Exception:  # noqa: BLE001
            pass

        # 按用户统计
        users: dict[str, dict] = {}
        for m in recent:
            uid = m["uid"] or "?"
            u = users.setdefault(
                uid,
                {
                    "uid": uid,
                    "name": m["name"] or uid,
                    "count": 0,
                    "chars": 0,
                    "imgs": 0,
                    "late": 0,
                },
            )
            if m["name"]:
                u["name"] = m["name"]
            u["count"] += 1
            u["chars"] += len(m["text"])
            u["imgs"] += m["img"]
            h = time.localtime(m["ts"]).tm_hour
            if h >= 23 or h < 5:
                u["late"] += 1

        total = len(recent)
        topics = self._extract_topics([m["text"] for m in recent if m["text"]])
        first_ts = min(m["ts"] for m in recent)
        last_ts = max(m["ts"] for m in recent)
        peak_hour = max(
            range(24),
            key=lambda h: sum(
                1 for m in recent if time.localtime(m["ts"]).tm_hour == h
            ),
        )
        ranking = sorted(users.values(), key=lambda u: u["count"], reverse=True)
        titles = self._assign_titles(users)
        total_imgs = sum(u["imgs"] for u in users.values())
        avg = total / max(len(users), 1)
        top_name = ranking[0]["name"] if ranking else ""
        summary = (
            f"最近 {hours} 小时共 {total} 条消息，{len(users)} 人参与，人均 {avg:.1f} 条；"
            f"{top_name} 最活跃。数据来源：{source_note}。"
        )
        # 纯文本版报告（图片模式提示词 / 回退时使用）
        report_lines = [
            f"📊 群分析（最近 {hours} 小时 · {source_note}）",
            f"统计时间：{time.strftime('%m-%d %H:%M', time.localtime(first_ts))} ~ "
            f"{time.strftime('%m-%d %H:%M', time.localtime(last_ts))}",
            f"共 {total} 条消息 · {len(users)} 人发言 · 发图 {total_imgs} 张 · 峰值时段 {peak_hour:02d}:00",
            "",
            "👑 龙王榜：",
        ]
        report_lines += [
            f"{i}. {u['name']} —— {u['count']} 条"
            for i, u in enumerate(ranking[:5], 1)
        ]
        report_lines += ["", "🏷️ 群友称号："]
        report_lines += [t for _, t in titles[:5]]
        report_lines += [
            "",
            "🔥 热门话题：" + ("、".join(topics) if topics else "暂无"),
            "📝 总结：" + summary,
        ]
        report_text = "\n".join(report_lines)

        # 结构化行（文字模式与本地长图共用；带真实头像的用户条目）
        rows: list[dict] = []

        def add_row(text: str, avatar_uid: str | None = None, kind: str = "text") -> None:
            rows.append({"kind": kind, "text": text, "avatar_uid": avatar_uid})

        add_row(f"📊 群聊分析报告（最近 {hours} 小时）", kind="title")
        add_row(
            f"📅 {time.strftime('%m-%d %H:%M', time.localtime(first_ts))} ~ "
            f"{time.strftime('%m-%d %H:%M', time.localtime(last_ts))}",
            kind="meta",
        )
        add_row(
            f"💬 共 {total} 条消息 · {len(users)} 人发言 · 发图 {total_imgs} 张 · 峰值时段 {peak_hour:02d}:00",
            kind="meta",
        )
        add_row("", kind="blank")
        add_row("👑 龙王榜：", kind="section")
        for i, u in enumerate(ranking[:5], 1):
            add_row(f"{i}. {u['name']} —— {u['count']} 条", u["uid"])
        add_row("", kind="blank")
        add_row("🏷️ 群友称号：", kind="section")
        for uid, title in titles[:5]:
            add_row(title, uid)
        add_row("", kind="blank")
        add_row("🔥 热门话题：" + ("、".join(topics) if topics else "暂无"))
        add_row("📝 总结：" + summary)

        # 图片模式：中转站生成报告长图（提示词要求不绘制头像/人脸），随后附真实头像名单
        if mode == "图片":
            await event.send(MessageChain([Plain("🖼️ 正在调用中转站生成群聊报告长图...")]))
            await self._report_as_image(event, report_text)
            # 真实头像附件：AI 海报画不了真人头像，补发带头像的名单消息（qlogo 真实头像）
            try:
                attach: list = [Plain("🖼️ 群成员真实头像（对应上方海报）：")]
                seen: set = set()
                for row in rows:
                    uid = row.get("avatar_uid")
                    if not uid or uid in seen:
                        continue
                    seen.add(uid)
                    url = self._avatar_url(event, uid)
                    if url:
                        attach.append(CompImage.fromURL(url))
                    if row.get("text"):
                        attach.append(Plain(row["text"]))
                if len(attach) > 1:
                    await event.send(MessageChain(attach))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"发送真实头像名单失败: {e}")
            return

        # 文字模式：组装带头像的消息链
        chain: list = []
        for row in rows:
            if row["avatar_uid"]:
                url = self._avatar_url(event, row["avatar_uid"])
                if url:
                    chain.append(CompImage.fromURL(url))
            if row["text"]:
                chain.append(Plain(row["text"]))
        await event.send(MessageChain(chain))

    def _persist_group_records(self, group_key: str) -> None:
        """将某群的记录整体写回持久化文件。"""
        try:
            path = self._group_file(group_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as f:
                for r in self._group_msgs[group_key]:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"写入群消息记录失败: {e}")

    @staticmethod
    def _row_to_record(row) -> dict | None:
        """把数据库 PlatformMessageHistory 行转换为群分析记录。"""
        try:
            content = getattr(row, "content", None) or []
            if isinstance(content, dict):
                content = [content]
            text_parts: list[str] = []
            img = 0
            for comp in content if isinstance(content, list) else []:
                if not isinstance(comp, dict):
                    continue
                ctype = str(comp.get("type", "")).lower()
                if "plain" in ctype or "text" in ctype:
                    text_parts.append(str(comp.get("text", "") or ""))
                elif "image" in ctype:
                    img += 1
            ts = getattr(row, "created_at", None)
            ts_val = ts.timestamp() if ts else time.time()
            return {
                "ts": ts_val,
                "uid": str(getattr(row, "sender_id", "") or "").strip() or "?",
                "name": str(getattr(row, "sender_name", "") or "").strip(),
                "text": "".join(text_parts).strip(),
                "img": img,
            }
        except Exception:  # noqa: BLE001
            return None

    def _find_archive_db(self) -> Path | None:
        """定位聊天存档插件（astrbot_plugin_chat_archive）的 SQLite 数据库。"""
        try:
            env = os.environ.get("ARCHIVE_DB_PATH", "").strip()
            if env:
                p = Path(env).expanduser()
                if p.is_file():
                    return p
            data_root = Path(get_astrbot_data_path())
            # 存档插件配置中的 basic.db_path
            try:
                cfg_path = (
                    data_root / "config" / "astrbot_plugin_chat_archive_config.json"
                )
                if cfg_path.is_file():
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    dbp = str((cfg.get("basic") or {}).get("db_path", "")).strip()
                    if dbp:
                        p = Path(dbp).expanduser()
                        if not p.is_absolute():
                            p = (
                                data_root
                                / "plugin_data"
                                / "astrbot_plugin_chat_archive"
                                / p
                            )
                        if p.is_file():
                            return p
            except Exception:  # noqa: BLE001
                pass
            # 默认路径
            cand = (
                data_root
                / "plugin_data"
                / "astrbot_plugin_chat_archive"
                / "chat_history.db"
            )
            if cand.is_file():
                return cand
            # 兜底：搜索 plugin_data 下的 chat_history.db
            for p in (data_root / "plugin_data").glob("*/chat_history.db"):
                if p.is_file():
                    return p
        except Exception:  # noqa: BLE001
            pass
        return None

    def _backfill_from_archive_db(
        self, db_path: str, platform: str, group_id: str
    ) -> int:
        """从聊天存档插件的 chat_history 表补录当前群的历史消息（同步，线程内执行）。"""
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            cur = conn.execute(
                "SELECT user_id, sender_name, message, timestamp FROM chat_history "
                "WHERE session_id LIKE '%:' || ? "
                "AND message_type LIKE '%roup%' "
                "AND (platform_id = ? OR platform_id IS NULL OR platform_id = '') "
                "ORDER BY timestamp DESC LIMIT 3000",
                (group_id, platform),
            )
            rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"查询存档数据库失败: {e}")
            rows = []
        finally:
            conn.close()
        if not rows:
            return 0
        deque_ = self._group_msgs.setdefault(
            group_id, collections.deque(maxlen=2000)
        )
        existing = {
            (m.get("uid"), m.get("text"), float(m.get("ts", 0))) for m in deque_
        }
        added = 0
        for uid, name, text, ts in reversed(rows):  # 转为正序
            if not uid or text is None:
                continue
            text = str(text).strip()
            ts_val = float(ts or 0)
            rec = {
                "ts": ts_val,
                "uid": str(uid).strip(),
                "name": str(name or "").strip(),
                "text": text,
                "img": 0,  # 存档库仅存文本
            }
            key = (rec["uid"], rec["text"], ts_val)
            if key in existing:
                continue
            existing.add(key)
            deque_.append(rec)
            added += 1
        return added

    async def _backfill_from_platform_history(
        self, event: AstrMessageEvent, group_key: str
    ) -> int:
        """回退方案：从 AstrBot 自带 platform_message_history 表补录。"""
        try:
            db = self.context.get_db()
            platform = event.get_platform_name()
            deque_ = self._group_msgs.setdefault(
                group_key, collections.deque(maxlen=2000)
            )
            existing = {
                (m.get("uid"), m.get("text"), float(m.get("ts", 0)))
                for m in deque_
            }
            added = 0
            page = 1
            while page <= 100:
                try:
                    rows = await db.get_platform_message_history(
                        platform, group_key, page=page, page_size=20
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"查询平台历史失败: {e}")
                    break
                if not rows:
                    break
                for row in rows:
                    rec = self._row_to_record(row)
                    if not rec:
                        continue
                    key = (rec["uid"], rec["text"], float(rec["ts"]))
                    if key in existing:
                        continue
                    existing.add(key)
                    deque_.append(rec)
                    added += 1
                page += 1
            return added
        except Exception as e:  # noqa: BLE001
            logger.warning(f"平台历史补录失败: {e}")
            return 0

    async def _backfill_history(self, event: AstrMessageEvent) -> None:
        """补录更早的群消息历史：优先聊天存档插件数据库，回退 AstrBot 自带历史表（仅管理员/主人）。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        if not (event.is_admin() or self._is_master(event)):
            await event.send(MessageChain([Plain("🚫 仅管理员或主人可执行补录。")]))
            return
        await event.send(MessageChain([Plain("🔄 正在补录历史消息...")]))
        try:
            group_key = str(event.message_obj.group_id or "")
            if not group_key:
                await event.send(MessageChain([Plain("⚠️ 无法获取当前群号。")]))
                return
            self._ensure_records_loaded()
            added = 0
            source = ""
            # 1) 聊天存档插件数据库（chat_history 表）
            archive_db = self._find_archive_db()
            if archive_db:
                try:
                    added = await asyncio.to_thread(
                        self._backfill_from_archive_db,
                        str(archive_db),
                        event.get_platform_name(),
                        group_key,
                    )
                    source = "聊天存档插件数据库"
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"从存档数据库补录失败: {e}")
            # 2) 回退：AstrBot 自带平台消息历史表
            if added == 0:
                added = await self._backfill_from_platform_history(event, group_key)
                if added:
                    source = "AstrBot 平台消息历史"
            if added:
                self._persist_group_records(group_key)
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                f"✅ 已从{source}补录 {added} 条历史消息（当前群）。\n"
                                "之后可用 群分析 查看统计。"
                            )
                        ]
                    )
                )
            else:
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                "⚠️ 未找到可补录的历史消息。\n"
                                "已检查：聊天存档插件数据库（astrbot_plugin_chat_archive）与 "
                                "AstrBot 平台消息历史。\n"
                                "请确认存档插件已安装且 enable_archive 已开启、群里有消息记录。"
                            )
                        ]
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            await event.send(MessageChain([Plain(f"😵 补录失败：{self._fmt_error(e)}")]))

    async def _report_as_image(self, event: AstrMessageEvent, report_text: str) -> None:
        """用当前中转站的图片接口生成报告长图（失败回退文字模式）。"""
        try:
            model, _note = await self._resolve_model()
            if not model:
                raise RuntimeError("未配置图片模型")
            prompt = (
                "请将以下群聊分析报告内容渲染成一张精美、可读的竖向长图海报："
                "中文排版清晰易读、配色美观、层次分明，适合手机竖屏查看，"
                "完整呈现所有条目，不要遗漏、不要编造内容。"
                "注意：只做文字排版，不要绘制任何头像、人脸或人物形象，"
                "不要添加任何与群成员相关的图片或插画：\n\n"
                + report_text
            )
            try:
                all_models = await self._get_image_models()
            except Exception:  # noqa: BLE001
                all_models = []
            candidates = [model] + [m for m in all_models if m != model][:3]
            size_override = (
                "1024x1024" if model.lower().startswith("dall-e-2") else "1024x1792"
            )
            out, used_model = await self._generate_with_fallback(
                candidates, prompt, None, size_override=size_override
            )
            await self._send_image(event, out, f"📊 群聊报告长图（{used_model}）")
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            await event.send(
                MessageChain(
                    [
                        Plain(
                            f"⚠️ 图片模式生成失败（{self._fmt_error(e)}），已回退文字模式：\n\n"
                            + report_text
                        )
                    ]
                )
            )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def record_group_listener(self, event: AstrMessageEvent):
        """（文档示例：事件类型过滤）仅接收群消息事件，用于群分析统计。"""
        try:
            self._record_group_message(event)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"记录群消息失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听所有消息：风格指令、免前缀指令解析、自定义生成指令入口、收集确认与收集模式处理。"""
        try:
            # 风格菜单选择（回复数字/分类名查看对应分类）
            if await self._handle_style_menu_select(event):
                return
            # 风格指令（手办化、Q版化 等，免 # 前缀）
            if await self._handle_style_command(event):
                return
            # 群聊中未唤醒的消息（无 / 前缀、无 @）：手动解析本插件指令，实现免 / 免 @
            if not event.is_at_or_wake_command:
                if await self._handle_unwoken_command(event):
                    return
            if await self._handle_custom_draw_entry(event):
                return
            if await self._handle_pending_confirm(event):
                return
            await self._process_collection(event)
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())

    def _group_file(self, group_key: str) -> Path:
        """群消息持久化文件路径（JSONL，每行一条记录）。"""
        safe = re.sub(r"[^0-9a-zA-Z_\-]", "_", group_key) or "unknown"
        return self._group_dir / f"{safe}.jsonl"

    def _ensure_records_loaded(self) -> None:
        """启动/重载时从持久化文件加载群消息记录（旧聊天记录不丢失）。"""
        if self._records_loaded:
            return
        self._records_loaded = True
        try:
            if not self._group_dir.is_dir():
                return
            for f in self._group_dir.glob("*.jsonl"):
                key = f.stem
                deque_ = collections.deque(maxlen=2000)
                try:
                    with f.open(encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:  # noqa: BLE001
                                continue
                            if isinstance(rec, dict) and "ts" in rec:
                                deque_.append(rec)
                except Exception:  # noqa: BLE001
                    pass
                if deque_:
                    self._group_msgs[key] = deque_
            total = sum(len(d) for d in self._group_msgs.values())
            if total:
                logger.info(f"群分析：已从持久化文件加载历史消息 {total} 条（{len(self._group_msgs)} 个群）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载群消息记录失败: {e}")

    def _record_group_message(self, event: AstrMessageEvent) -> None:
        """记录群消息用于群分析（文档方式：GROUP_MESSAGE 事件监听 + message_obj）。

        按 message_obj.group_id 归属群；图片数从消息链 message 中数 Image 段；
        内存 + 持久化到文件（重载/重启保留），每群上限 2000 条。
        """
        try:
            if not self.config.get("report_enabled", True):
                return
            if event.is_private_chat():
                return
            self._ensure_records_loaded()
            group_key = str(event.message_obj.group_id or "") or event.unified_msg_origin
            if group_key not in self._group_msgs:
                self._group_msgs[group_key] = collections.deque(maxlen=2000)
            img = sum(
                1
                for c in getattr(event.message_obj, "message", []) or []
                if isinstance(c, CompImage)
                or str(getattr(c, "type", "")).lower() == "image"
            )
            rec = {
                "ts": getattr(event.message_obj, "timestamp", None) or time.time(),
                "uid": str(event.get_sender_id() or "").strip(),
                "name": event.get_sender_name() or "",
                "text": event.message_str.strip(),
                "img": img,
            }
            self._group_msgs[group_key].append(rec)
            # 持久化：JSONL 追加写；每 2500 条重写一次文件（保留最近 2000 条）
            try:
                path = self._group_file(group_key)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._write_counters[group_key] = (
                    self._write_counters.get(group_key, 0) + 1
                )
                if self._write_counters[group_key] >= 2500:
                    self._write_counters[group_key] = 0
                    with path.open("w", encoding="utf-8") as f:
                        for r in self._group_msgs[group_key]:
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"持久化群消息失败: {e}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"记录群消息失败: {e}")

    async def _handle_style_command(self, event: AstrMessageEvent) -> bool:
        """处理风格指令（手办化、Q版化 等），免 # 前缀。

        带图/引用直接进入收集模式；纯文字先确认（60 秒有效）。
        """
        msg = event.message_str.strip()
        if not msg:
            return False
        parts = msg.split(" ", 1)
        key = parts[0].strip()
        extra = parts[1].strip() if len(parts) > 1 else ""
        prompt = STYLE_COMMANDS.get(key)
        if not prompt:
            return False
        if not self._user_allowed(event):
            logger.debug(f"风格指令：无权限用户触发 {key!r}，已忽略")
            return False
        if extra:
            prompt = f"{prompt}\n{extra}"
        has_media = bool(self._collect_direct_images(event)) or any(
            isinstance(c, Reply) or str(getattr(c, "type", "")).lower() == "reply"
            for c in event.get_messages()
        )
        event.should_call_llm(False)
        if has_media:
            logger.info(f"风格指令：{key}（带图）进入收集")
            await self._enter_collection(event, prompt)
        else:
            self._pending_collect[event.unified_msg_origin] = (
                prompt,
                time.time(),
                str(event.get_sender_id() or "").strip(),
            )
            await event.send(
                MessageChain(
                    [
                        Plain(
                            f"🤔 使用风格「{key}」进入图片收集模式？\n"
                            "回复「确认」进入，「取消」退出。（60 秒内有效）"
                        )
                    ]
                )
            )
        event.stop_event()
        return True

    async def _handle_style_menu_select(self, event: AstrMessageEvent) -> bool:
        """处理风格菜单选择：回复「数字」或「分类名」查看对应分类；「取消」退出。

        仅菜单发起者（或主人）可操作；120 秒未选择自动作废。
        """
        pending = self._pending_style_menu.get(event.unified_msg_origin)
        if not pending:
            return False
        ts, owner = pending
        if time.time() - ts > 120:
            self._pending_style_menu.pop(event.unified_msg_origin, None)
            return False
        sender = str(event.get_sender_id() or "").strip()
        if owner and sender and sender != owner:
            if sender not in self._ids(self.config.get("master_ids")):
                return False
        msg = event.message_str.strip()
        if not msg:
            return False
        if msg in ("取消", "退出"):
            self._pending_style_menu.pop(event.unified_msg_origin, None)
            event.should_call_llm(False)
            event.stop_event()
            await event.send(MessageChain([Plain("🚫 已退出风格菜单。")]))
            return True
        # 数字或分类名匹配
        cat = None
        try:
            idx = int(msg)
            if 1 <= idx <= len(STYLE_CATEGORIES):
                cat = STYLE_CATEGORIES[idx - 1]
        except (TypeError, ValueError):
            pass
        if cat is None:
            for name, keys in STYLE_CATEGORIES:
                if msg in (name, name + "类", name.rstrip("类")):
                    cat = (name, keys)
                    break
        if cat is None:
            return False  # 不匹配则不消费，保持等待
        self._pending_style_menu.pop(event.unified_msg_origin, None)
        event.should_call_llm(False)
        event.stop_event()
        name, keys = cat
        lines = [f"🎨 {name}（{len(keys)} 个）："]
        lines += [f"{i}. {k} —— {STYLE_COMMANDS[k]}" for i, k in enumerate(keys, 1)]
        await event.send(MessageChain([Plain("\n".join(lines))]))
        return True

    async def _handle_pending_confirm(self, event: AstrMessageEvent) -> bool:
        """处理收集模式进入前的确认（免前缀误触防护）。

        仅发起者（或主人）可确认/取消；60 秒未确认自动作废。
        """
        pending = self._pending_collect.get(event.unified_msg_origin)
        if not pending:
            return False
        args, ts, owner = pending
        if time.time() - ts > 60:
            self._pending_collect.pop(event.unified_msg_origin, None)
            return False
        sender = str(event.get_sender_id() or "").strip()
        if owner and sender and sender != owner:
            if sender not in self._ids(self.config.get("master_ids")):
                return False
        msg = event.message_str.strip()
        if msg == "确认":
            self._pending_collect.pop(event.unified_msg_origin, None)
            event.should_call_llm(False)
            await self._enter_collection(event, args)
            event.stop_event()
            return True
        if msg == "取消":
            self._pending_collect.pop(event.unified_msg_origin, None)
            event.should_call_llm(False)
            event.stop_event()
            await event.send(MessageChain([Plain("🚫 已取消。")]))
            return True
        return False

    async def _handle_unwoken_command(self, event: AstrMessageEvent) -> bool:
        """手动解析群聊中未唤醒的插件指令（免 / 前缀、免 @ 机器人）。

        为避免聊天误判，无参数指令（模型、指令列表等）要求整条消息完全等于指令名；
        其余带参数指令仍按首个词匹配。
        """
        if not self.config.get("unwoken_commands", True):
            return False
        msg = event.message_str.strip()
        if not msg:
            return False
        parts = msg.split(" ", 1)
        first = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""

        handler = None
        if first in ("绘图", "画画", "生成图片", "draw", "img"):
            # 收集入口必须校验权限：未授权用户的聊天消息（如"画画 真好玩"）不得触发收集
            if not self._user_allowed(event):
                logger.debug(f"免前缀指令：无权限用户触发 {first!r}，已忽略")
                return False
            # 消息带图片/引用 -> 意图明确，直接进入收集；纯文字 -> 先确认，防聊天误触
            has_media = bool(self._collect_direct_images(event)) or any(
                isinstance(c, Reply) or str(getattr(c, "type", "")).lower() == "reply"
                for c in event.get_messages()
            )
            if has_media:
                handler = self._enter_collection(event, rest)
            else:
                self._pending_collect[event.unified_msg_origin] = (
                    rest,
                    time.time(),
                    str(event.get_sender_id() or "").strip(),
                )
                event.should_call_llm(False)
                event.stop_event()
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                "🤔 是要进入图片收集模式吗？\n"
                                "回复「确认」进入，「取消」退出。（60 秒内有效）"
                            )
                        ]
                    )
                )
                return True
        elif first in ("白名单", "加白名单"):
            handler = self._manage_user_list(event, "whitelist_ids", "白名单", rest)
        elif first in ("黑名单", "加黑名单"):
            handler = self._manage_user_list(event, "blacklist_ids", "黑名单", rest)
        elif first in ("模型", "获取模型", "刷新模型"):
            # 无参数指令：要求整条消息完全等于指令名，避免聊天误触发
            if rest:
                logger.debug(f"免前缀指令：{first!r} 带多余文本 {rest!r}，不触发")
                return False
            handler = self.list_models(event)
        elif first in ("设置模型", "切换模型"):
            handler = self.set_model(event, GreedyStr(rest))
        elif first in ("设置尺寸", "切换尺寸"):
            handler = self.set_size(event, GreedyStr(rest))
        elif first in ("供应商", "站点", "切换供应商"):
            handler = self.list_stations(event, GreedyStr(rest))
        elif first in ("更新日志", "更新记录", "changelog"):
            handler = self.changelog(event)
        elif first in ("风格列表", "样式列表"):
            handler = self.style_list(event)
        elif first in ("群分析", "群聊报告", "分析报告", "群聊分析", "龙王榜"):
            handler = self.group_report(event, GreedyStr(rest))
        elif first in ("指令列表", "指令", "命令"):
            # 无参数指令：要求整条消息完全等于指令名
            if rest:
                logger.debug(f"免前缀指令：{first!r} 带多余文本 {rest!r}，不触发")
                return False
            handler = self.cmd_list(event)
        if handler is None:
            return False
        logger.info(f"免前缀指令：first={first!r} rest={rest!r}")
        event.should_call_llm(False)
        try:
            await handler
        finally:
            event.stop_event()
        return True

    # ------------------------------------------------------------------
    # 自动撤回状态消息（QQ/OneBot）
    # ------------------------------------------------------------------

    def _auto_recall_enabled(self) -> bool:
        """是否开启自动撤回状态消息（配置项「自动撤回状态消息」，默认开）。"""
        return bool(self.config.get("auto_recall_status", True))

    @staticmethod
    def _onebot_call_fn(event: AstrMessageEvent):
        """解析平台协议端的 call_action 可调用对象（OneBot v11 等）。

        返回 callable 或 None；其它平台（微信/Telegram 等）没有该接口时返回 None，
        自动撤回会静默降级为不撤回。
        """
        bot = getattr(event, "bot", None)
        fn = getattr(bot, "call_action", None)
        if not callable(fn):
            api = getattr(bot, "api", None)
            fn = getattr(api, "call_action", None)
        return fn if callable(fn) else None

    @staticmethod
    def _unwrap_action(data):
        """兼容不同协议端返回：{'data': {...}} 与直接 {...} 两种形态均归一为 data。"""
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data

    async def _onebot_call(self, event: AstrMessageEvent, action: str, **params):
        """调用平台协议端 API；无该接口或失败时返回 None（静默）。"""
        fn = self._onebot_call_fn(event)
        if fn is None:
            return None
        try:
            result = await fn(action=action, **params)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"协议端调用 {action} 失败: {e}")
            return None
        return self._unwrap_action(result)

    async def _send_status(self, event: AstrMessageEvent, text: str):
        """发送一段状态文本；支持撤回的平台返回消息 ID，否则返回 None。

        QQ/OneBot：直接走协议端 send_group_msg / send_private_msg 以拿到 message_id，
        便于后续自动撤回；其它平台回退 event.send（不支持撤回）。
        """
        if self._auto_recall_enabled() and self._onebot_call_fn(event) is not None:
            segments = [{"type": "text", "data": {"text": text}}]
            params = {}
            self_id = event.get_self_id()
            if self_id:
                params["self_id"] = self_id
            try:
                group_id = str(event.get_group_id() or "").strip()
                if group_id:
                    if group_id.isdigit():
                        params["group_id"] = int(group_id)
                    else:
                        params["group_id"] = group_id
                    ret = await self._onebot_call(
                        event, "send_group_msg", message=segments, **params
                    )
                else:
                    sender = str(event.get_sender_id() or "").strip()
                    if not sender:
                        await event.send(MessageChain([Plain(text)]))
                        return None
                    if sender.isdigit():
                        params["user_id"] = int(sender)
                    else:
                        params["user_id"] = sender
                    ret = await self._onebot_call(
                        event, "send_private_msg", message=segments, **params
                    )
                if isinstance(ret, dict):
                    mid = ret.get("message_id")
                    if mid is not None:
                        return mid
            except Exception as e:  # noqa: BLE001
                logger.debug(f"协议端直接发送状态消息失败，回退 event.send: {e}")
        await event.send(MessageChain([Plain(text)]))
        return None

    async def _recall_message(self, event: AstrMessageEvent, message_id) -> None:
        """撤回一条消息（仅支持撤回的平台生效；失败静默忽略）。"""
        if message_id is None:
            return
        if not self._auto_recall_enabled() or self._onebot_call_fn(event) is None:
            return
        params = {"message_id": message_id}
        self_id = event.get_self_id()
        if self_id:
            params["self_id"] = self_id
        await self._onebot_call(event, "delete_msg", **params)

    async def _recall_messages(self, event: AstrMessageEvent, ids) -> None:
        """按顺序撤回一批消息。"""
        for mid in list(ids):
            await self._recall_message(event, mid)

    # ------------------------------------------------------------------
    # 收集模式
    # ------------------------------------------------------------------

    async def _enter_collection(self, event: AstrMessageEvent, args_text: str) -> None:
        """进入收集模式：以当前消息的指令参数、附带图片、引用内容作为初始素材。"""
        quoted_text, quoted_images = await self._collect_quoted(event)
        direct_images = self._collect_direct_images(event)
        images: list[list[str]] = list(direct_images)
        # 引用图片与直接附图可能是同一张（不同引用形式），合并去重
        self._merge_image_candidates(images, [[r] for r in quoted_images])
        # 立即将图片落盘到插件持久目录，避免「开始」时 URL 过期/临时文件被清理
        await self._persist_collected_images(event, images)
        texts = [t for t in (args_text, quoted_text) if t]
        session = CollectSession(
            umo=event.unified_msg_origin,
            prompt="\n".join(texts).strip(),
            images=images,
            texts=[],
            owner_id=str(event.get_sender_id() or "").strip(),
            created_at=time.time(),
            last_active=time.time(),
        )
        self._collect[event.unified_msg_origin] = session
        lines = ["📥 已进入收集模式"]
        if session.prompt:
            lines.append(f"📝 初始文字：{len(session.prompt)} 字")
        if session.images:
            lines.append(f"🖼️ 已收集图片：{len(session.images)} 张")
        if not event.is_private_chat():
            lines.append("✅ 群聊中直接发送指令/素材即可，无需 / 前缀或 @ 机器人")
        lines.append("请继续发送素材（图片/文字）；发送「开始」开始生成，「继续」继续收集，「取消」退出。")
        lines.append("（仅发起者或主人可投递素材与控制，他人消息不受影响）")
        msg_id = await self._send_status(event, "\n".join(lines))
        if msg_id is not None:
            session.sent_msg_ids.append(msg_id)

    def _custom_draw_name(self) -> str | None:
        """返回用户自定义的生成指令名；与默认指令/别名重复时返回 None。"""
        name = str(self.config.get("draw_command", "") or "").strip()
        if not name or name in OUR_COMMANDS:
            return None
        return name

    async def _handle_custom_draw_entry(self, event: AstrMessageEvent) -> bool:
        """处理自定义生成指令（如 /绘图 xxx）进入收集模式，返回是否已处理。"""
        name = self._custom_draw_name()
        if not name:
            return False
        msg = event.message_str.strip()
        if msg != name and not msg.startswith(name + " "):
            return False
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            event.stop_event()
            return True
        await self._enter_collection(event, msg[len(name):].strip())
        event.stop_event()
        return True

    async def _process_collection(self, event: AstrMessageEvent) -> None:
        """处理收集模式下的素材收集与控制词（开始/继续/取消）。"""
        session = self._collect.get(event.unified_msg_origin)
        if not session:
            return
        if not self._user_allowed(event):
            # 无权限用户：不收集、不响应（静默忽略）
            logger.debug(f"收集模式：忽略无权限用户的消息")
            return
        # 仅发起者（或主人）可投递素材与控制，其他人消息静默忽略（不影响群聊）
        owner = session.owner_id or ""
        sender = str(event.get_sender_id() or "").strip()
        if owner and sender and sender != owner:
            if sender not in self._ids(self.config.get("master_ids")):
                logger.debug(f"收集模式：忽略非发起者 {sender} 的消息")
                return
        if session.generating:
            # 生成中：不收集、不响应任何消息
            logger.debug(f"收集模式：生成中，忽略消息")
            return
        now = time.time()
        if now - session.last_active > COLLECT_IDLE_TIMEOUT:
            # 空闲超时自动退出收集模式
            logger.info(f"收集模式超时退出: {event.unified_msg_origin}")
            self._collect.pop(event.unified_msg_origin, None)
            await self._recall_messages(event, session.sent_msg_ids)
            session.sent_msg_ids.clear()
            return
        msg = event.message_str.strip()
        # 插件自身指令（含自定义生成指令）不作为素材
        names = set(OUR_COMMANDS)
        custom = self._custom_draw_name()
        if custom:
            names.add(custom)
        if any(msg == n or msg.startswith(n + " ") for n in names):
            logger.debug(f"收集模式：跳过插件指令消息 {msg!r}")
            return
        if session.paused and msg not in ("开始", "继续", "取消"):
            # 暂停中（生成失败后）：普通消息不收集、不响应
            logger.debug(f"收集模式：暂停中，忽略消息")
            return

        if msg == "取消":
            self._collect.pop(event.unified_msg_origin, None)
            event.should_call_llm(False)
            event.stop_event()
            # 撤回收集期间发送的状态消息，保持聊天整洁
            await self._recall_messages(event, session.sent_msg_ids)
            session.sent_msg_ids.clear()
            await event.send(MessageChain([Plain("🚫 已取消收集模式，素材已清空。")]))
            return
        if msg == "继续":
            session.last_active = now
            session.paused = False
            event.should_call_llm(False)
            event.stop_event()
            msg_id = await self._send_status(
                event,
                f"📥 继续收集中：当前 图片 {len(session.images)} 张 / 文字 {len(session.texts)} 段；"
                "「开始」生成，「取消」退出。",
            )
            if msg_id is not None:
                session.sent_msg_ids.append(msg_id)
            return
        if msg == "开始":
            session.last_active = now
            event.should_call_llm(False)
            event.stop_event()
            # 收集完成（发送「开始」后）：撤回收集期间的所有状态消息
            await self._recall_messages(event, session.sent_msg_ids)
            session.sent_msg_ids.clear()
            session.generating = True  # 生成期间锁定，不再收集任何消息
            ok = await self._start_from_collection(event, session)
            if ok:
                # 生成成功：退出收集模式
                self._collect.pop(event.unified_msg_origin, None)
            else:
                # 生成失败：保留素材但暂停收集，避免继续吞聊天消息
                session.last_active = time.time()
                session.generating = False
                session.paused = True
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                "📥 生成失败，素材已保留（已暂停收集聊天消息）。\n"
                                "发送「继续」继续收集素材，或直接再次发送「开始」重试，「取消」退出。"
                            )
                        ]
                    )
                )
            return

        # 素材收集：图片 + 文字
        images = self._collect_direct_images(event)
        if not images and not msg:
            # 无图无文字（表情/语音/卡片等）：收集模式下静默吞掉，不触发默认对话
            event.should_call_llm(False)
            event.stop_event()
            logger.debug(f"收集模式：静默忽略无素材消息")
            return
        if msg:
            session.texts.append(msg)
        # 先落盘：把持久路径插入候选列表后，再合并进收集状态（合并会复制候选列表，
        # 若先合并后落盘，持久路径只进局部列表，'开始' 时 session 里仍是会失效的临时路径）
        await self._persist_collected_images(event, images)
        self._merge_image_candidates(session.images, images)
        session.last_active = now
        event.should_call_llm(False)
        event.stop_event()
        logger.info(
            f"收集模式：已收集素材 msg={msg!r} 新增图片候选 {images}，"
            f"当前共 图片 {len(session.images)} 张 / 文字 {len(session.texts)} 段"
        )
        lines = [
            f"✅ 已收集：图片 {len(session.images)} 张，"
            f"文字 {len(session.texts)} 段（共 {sum(len(t) for t in session.texts)} 字）"
        ]
        lines.append("「开始」生成，「继续」继续，「取消」退出。")
        msg_id = await self._send_status(event, "\n".join(lines))
        if msg_id is not None:
            session.sent_msg_ids.append(msg_id)

    async def _start_from_collection(
        self, event: AstrMessageEvent, session: CollectSession
    ) -> bool:
        """根据收集到的素材开始生成。返回是否成功（失败时素材会保留）。"""
        prompt_final = "\n".join(
            p for p in [session.prompt, *session.texts] if p
        ).strip()
        edit_image_path = None
        if session.images:
            # 使用最新收集到的图片作为编辑底图（用户后发的图优先级最高）；
            # 每张图有多个候选引用，逐个尝试解析（兼容 OneBot/QQ 等平台）
            for candidates in reversed(session.images):
                for ref in candidates:
                    edit_image_path = await self._resolve_image(event, ref)
                    if edit_image_path:
                        break
                if edit_image_path:
                    break
            if not edit_image_path:
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                "😵 未能获取已收集的图片，请重新发送图片（建议直接在消息中附带图片）或取消。"
                            )
                        ]
                    )
                )
                return False
        if not edit_image_path and not prompt_final:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            "⚠️ 还没有任何素材：请先发送图片或文字，再发送「开始」；或发送「取消」退出。"
                        )
                    ]
                )
            )
            return False
        if edit_image_path and not prompt_final:
            await event.send(
                MessageChain(
                    [Plain("⚠️ 还缺少文字提示词：请发送文字后再「开始」；或发送「取消」退出。")]
                )
            )
            return False
        return await self._run_generation(event, prompt_final, edit_image_path)

    async def _run_generation(
        self,
        event: AstrMessageEvent,
        prompt_final: str,
        edit_image_path: str | None,
    ) -> bool:
        """模型解析 + 生成/编辑 + 发送结果（含 429 自动换模型、窗口限流排队）。

        返回是否成功发送了图片。
        """
        try:
            # 生成任务限流：窗口内任务数达到上限时排队等待
            wait = self._task_slot_wait()
            if wait > 0:
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                f"⏳ 生成任务繁忙（限流窗口内已达上限），已排队，"
                                f"约 {int(wait) + 1} 秒后开始..."
                            )
                        ]
                    )
                )
            await self._acquire_task_slot()

            model, model_note = await self._resolve_model()
            if not model:
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                "⚠️ 未配置图片模型，且无法从供应商自动获取。请检查 API 地址 / Key 配置，"
                                "或用 /模型 获取模型列表后以 /设置模型 指定。"
                            )
                        ]
                    )
                )
                return False

            # 候选模型：当前模型 + 供应商列表中其余图像模型（遇图片额度 429 时自动换模型重试）
            try:
                all_models = await self._get_image_models()
            except Exception:  # noqa: BLE001
                all_models = []
            candidates = [model] + [m for m in all_models if m != model][:3]

            progress_id = None
            if edit_image_path:
                progress = f"🎨 正在编辑图片（模型：{model}）..."
                if model_note:
                    progress += f"\n{model_note}"
                progress_id = await self._send_status(event, progress)
                try:
                    out, used_model = await self._generate_with_fallback(
                        candidates, prompt_final, edit_image_path
                    )
                except RuntimeError as e:
                    if self._is_edit_unsupported_error(e):
                        # 供应商不支持编辑接口：回退为仅文字生成，并明确告知用户
                        await event.send(
                            MessageChain(
                                [
                                    Plain(
                                        "⚠️ 当前供应商似乎不支持图片编辑接口（/images/edits），"
                                        "已改为仅文字生成（未使用你发的图片）。"
                                    )
                                ]
                            )
                        )
                        out, used_model = await self._generate_with_fallback(
                            candidates, prompt_final, None
                        )
                    else:
                        raise
            else:
                progress = f"🎨 正在生成图片（模型：{model}）..."
                if model_note:
                    progress += f"\n{model_note}"
                progress_id = await self._send_status(event, progress)
                out, used_model = await self._generate_with_fallback(
                    candidates, prompt_final, None
                )

            # 生成即将完成、结果马上发出前：撤回「正在生成/编辑」进度消息，保持聊天整洁
            await self._recall_message(event, progress_id)

            caption = f"✨ 已生成（{used_model}）"
            if used_model != model:
                # 自动切换了模型：保存并告知用户
                self.config["model"] = used_model
                self._save_config()
                caption += f"\nℹ️ 模型 {model} 图片额度不可用，已自动切换为 {used_model}"
            await self._send_image(event, out, caption)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            await event.send(MessageChain([Plain(f"😵 出错了：{self._fmt_error(e)}")]))
            return False

    @filter.command("模型", alias={"获取模型", "刷新模型"})
    async def list_models(self, event: AstrMessageEvent):
        """🔍 获取当前供应商支持的图像模型列表，并自动保存到配置。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        await event.send(MessageChain([Plain("🔍 正在从供应商拉取模型列表...")]))
        try:
            models = await self._get_image_models(force=True)
        except Exception as e:  # noqa: BLE001
            logger.error(traceback.format_exc())
            await event.send(MessageChain([Plain(f"😵 获取模型失败：{self._fmt_error(e)}")]))
            return
        if not models:
            await event.send(
                MessageChain([Plain("⚠️ 未发现可用模型。请检查供应商、API 地址与 Key 配置后重试。")])
            )
            return

        # 保存获取到的模型列表，并自动填入当前模型（若未设置或已失效）
        self.config["fetched_models"] = models
        current = self._model()
        if not current or current not in models:
            self.config["model"] = models[0]
            current = models[0]
        self._save_config()

        lines = [f"📋 图像模型列表（{len(models)} 个）："]
        lines += [f"{i + 1}. {m}" for i, m in enumerate(models[:30])]
        if len(models) > 30:
            lines.append(f"... 共 {len(models)} 个")
        lines.append(f"🎯 当前模型：{current}")
        lines.append("💡 使用 /设置模型 <模型名> 切换，或直接在配置面板中填写。")
        await event.send(MessageChain([Plain("\n".join(lines))]))

    @filter.command("设置模型", alias={"切换模型"})
    async def set_model(self, event: AstrMessageEvent, model_name: GreedyStr):
        """⚙️ 设置图片生成模型（仅管理员/主人）。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        if not (event.is_admin() or self._is_master(event)):
            await event.send(MessageChain([Plain("🚫 仅管理员或主人可使用此指令。")]))
            return
        name = str(model_name).strip()
        if not name:
            await event.send(
                MessageChain([Plain("💡 用法：/设置模型 <模型名>，如 /设置模型 gpt-image-1。可用 /模型 查看可用的图像模型。")])
            )
            return
        self.config["model"] = name
        self._save_config()
        await event.send(MessageChain([Plain(f"⚙️✅ 已设置图片模型：{name}")]))

    @filter.command("设置尺寸", alias={"切换尺寸"})
    async def set_size(self, event: AstrMessageEvent, size: GreedyStr):
        """📐 切换图片尺寸（仅管理员/主人）。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        if not (event.is_admin() or self._is_master(event)):
            await event.send(MessageChain([Plain("🚫 仅管理员或主人可使用此指令。")]))
            return
        value = str(size).strip().lower()
        if value == "自动":
            value = "auto"
        if not value:
            # 展示当前尺寸与可选列表
            current = str(self.config.get("size", "") or "").strip() or "1024x1024"
            if current == "auto":
                current = "auto（自动）"
            lines = [f"📐 当前尺寸：{current}"]
            lines.append(
                "可用尺寸："
                + "、".join(
                    "auto（自动）" if v == "auto" else v for v in SIZE_OPTIONS
                )
            )
            lines.append("💡 用法：/设置尺寸 <尺寸>，如 /设置尺寸 1024x1792 或 /设置尺寸 自动")
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return
        if value not in SIZE_OPTIONS:
            await event.send(
                MessageChain(
                    [
                        Plain(
                            f"⚠️ 尺寸 {value} 不受支持。可用："
                            + "、".join(
                                "auto（自动）" if v == "auto" else v
                                for v in SIZE_OPTIONS
                            )
                        )
                    ]
                )
            )
            return
        self.config["size"] = value
        self._save_config()
        shown = "auto（自动）" if value == "auto" else value
        await event.send(MessageChain([Plain(f"📐✅ 已设置图片尺寸：{shown}")]))

    @filter.command("供应商", alias={"站点", "切换供应商"})
    async def list_stations(self, event: AstrMessageEvent, station_name: GreedyStr):
        """📡 查看/添加/删除/切换/重命名中转站站点（仅管理员/主人）。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        if not (event.is_admin() or self._is_master(event)):
            await event.send(MessageChain([Plain("🚫 仅管理员或主人可使用此指令。")]))
            return
        name = str(station_name).strip()
        stations = self.config.get("stations") or []
        if name.startswith("添加"):
            # 添加站点：/供应商 添加 <站点名称> <API地址> [API Key]
            parts = name[len("添加"):].strip().split()
            if len(parts) < 2:
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                "💡 用法：/供应商 添加 <站点名称> <API地址> [API Key]\n"
                                "如：/供应商 添加 备用站 https://api.xxx.com sk-xxxx"
                            )
                        ]
                    )
                )
                return
            sname = parts[0]
            base = parts[1]
            key = " ".join(parts[2:]).strip()
            if not base.startswith(("http://", "https://")):
                await event.send(MessageChain([Plain("⚠️ API 地址需以 http:// 或 https:// 开头。")]))
                return
            for s in stations:
                if isinstance(s, dict) and str(s.get("name", "") or "").strip() == sname:
                    await event.send(
                        MessageChain([Plain(f"⚠️ 已存在同名站点 {sname}，请更换名称或先删除。")])
                    )
                    return
            stations.append(
                {
                    "__template_key": "station",
                    "name": sname,
                    "base": self._clean_base(base),
                    "key": key,
                }
            )
            self.config["stations"] = stations
            changed = False
            if not str(self.config.get("active_station", "") or "").strip():
                # 还没有当前站点：自动设为新添加的站点
                self.config["active_station"] = sname
                changed = True
            self._save_config()
            logger.info(f"站点添加：{sname} -> {self._clean_base(base)}")
            await event.send(
                MessageChain([Plain(f"📡➕ 已添加站点：{sname} —— {self._clean_base(base)}")])
            )
            return
        if name.startswith("删除") or name.startswith("移除"):
            # 删除站点：/供应商 删除 <站点名称>
            kw_len = len("删除") if name.startswith("删除") else len("移除")
            target = name[kw_len:].strip()
            if not target:
                await event.send(MessageChain([Plain("💡 用法：/供应商 删除 <站点名称>")]))
                return
            for i, s in enumerate(stations):
                if isinstance(s, dict) and str(s.get("name", "") or "").strip() == target:
                    del stations[i]
                    self.config["stations"] = stations
                    if str(self.config.get("active_station", "") or "").strip() == target:
                        # 删除的是当前站点：自动切换到第一个剩余站点
                        first_name = (
                            str(stations[0].get("name", "") or "").strip()
                            if stations and isinstance(stations[0], dict)
                            else ""
                        )
                        self.config["active_station"] = first_name
                    self._save_config()
                    logger.info(f"站点删除：{target}")
                    await event.send(MessageChain([Plain(f"📡🗑️ 已删除站点：{target}")]))
                    return
            await event.send(
                MessageChain([Plain(f"⚠️ 未找到名为 {target} 的站点。可用 /供应商 查看全部站点。")])
            )
            return
        if name.startswith("重命名"):
            # 重命名当前站点：/供应商 重命名 <新名称>
            new_name = name[len("重命名"):].strip()
            if not new_name:
                await event.send(
                    MessageChain([Plain("💡 用法：/供应商 重命名 <新名称>（重命名当前站点）。")])
                )
                return
            if not stations:
                await event.send(
                    MessageChain([Plain("⚠️ 尚未配置任何站点。请在配置面板的「站点列表」中添加中转站。")])
                )
                return
            active = str(self.config.get("active_station", "") or "").strip()
            idx = 0
            if active:
                for i, s in enumerate(stations):
                    if isinstance(s, dict) and str(s.get("name", "") or "").strip() == active:
                        idx = i
                        break
            target = stations[idx] if idx < len(stations) else None
            if not isinstance(target, dict):
                await event.send(MessageChain([Plain("⚠️ 站点数据异常，请到配置面板检查。")]))
                return
            old_name = str(target.get("name", "") or "").strip() or f"站点{idx + 1}"
            target["name"] = new_name
            if active and old_name == active:
                # 重命名的是当前站点：同步更新「当前站点」指向
                self.config["active_station"] = new_name
            self._save_config()
            await event.send(
                MessageChain([Plain(f"📡✏️ 站点已重命名：{old_name} -> {new_name}")])
            )
            return
        if name:
            # 切换站点
            for s in stations:
                if isinstance(s, dict) and str(s.get("name", "") or "").strip() == name:
                    self.config["active_station"] = name
                    self._save_config()
                    await event.send(MessageChain([Plain(f"📡✅ 已切换到站点：{name}")]))
                    return
            await event.send(
                MessageChain([Plain(f"⚠️ 未找到名为 {name} 的站点。可用 /供应商 查看全部站点。")])
            )
            return
        # 列出站点
        if not stations:
            await event.send(
                MessageChain([Plain("⚠️ 尚未配置任何站点。请在配置面板的「站点列表」中添加中转站。")])
            )
            return
        active = str(self.config.get("active_station", "") or "").strip()
        lines = [f"📡 站点列表（共 {len(stations)} 个）："]
        for i, s in enumerate(stations, 1):
            if not isinstance(s, dict):
                continue
            sname = str(s.get("name", "") or "").strip() or f"站点{i}"
            mark = " 🎯" if (sname == active or (not active and i == 1)) else ""
            lines.append(f"{i}. {sname}{mark}")
        lines.append(f"🎯 当前站点：{self._active_station_name()}")
        lines.append(
            "💡 用法：/供应商 <站点名称> 切换；添加 <名称> <地址> [Key]；删除 <名称>；重命名 <新名称>。"
        )
        await event.send(MessageChain([Plain("\n".join(lines))]))

    @filter.command("白名单", alias={"加白名单"})
    async def whitelist_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """⚪ 管理白名单：/白名单 @用户或ID 添加；/白名单 移除@用户 删除；无参查看。"""
        await self._manage_user_list(event, "whitelist_ids", "白名单", str(args).strip())

    @filter.command("黑名单", alias={"加黑名单"})
    async def blacklist_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """⚫ 管理黑名单：/黑名单 @用户或ID 添加；/黑名单 移除@用户 删除；无参查看。"""
        await self._manage_user_list(event, "blacklist_ids", "黑名单", str(args).strip())

    @staticmethod
    def _is_valid_user_id(uid: str) -> bool:
        """判断是否为合法的用户 ID：纯数字（QQ 等），或含 @ 与 . 的平台 ID（微信/Matrix 等）。"""
        uid = (uid or "").strip()
        if not uid:
            return False
        if re.fullmatch(r"\d+", uid):
            return True
        return "@" in uid and "." in uid

    async def _manage_user_list(
        self, event: AstrMessageEvent, key: str, label: str, text: str
    ) -> None:
        """白名单/黑名单指令处理：支持 @ 用户、直接填 ID、移除/删除、无参查看。"""
        event.should_call_llm(False)
        if not self._user_allowed(event):
            await self._reject_unauthorized(event)
            return
        if not (event.is_admin() or self._is_master(event)):
            await event.send(MessageChain([Plain(f"🚫 仅管理员或主人可管理{label}。")]))
            return

        # 收集消息中 @ 的用户 ID（排除 @全体成员）
        at_ids: list[str] = []
        for comp in event.get_messages():
            # 兼容各种 At 表示：isinstance / 类名 / type 枚举字符串
            is_at = (
                isinstance(comp, CompAt)
                or comp.__class__.__name__.lower() == "at"
                or "at" in str(getattr(comp, "type", "")).lower()
            )
            if not is_at:
                continue
            qq = str(getattr(comp, "qq", "") or "").strip()
            if not qq or qq.lower() == "all":
                continue
            if qq not in at_ids:
                at_ids.append(qq)
        logger.info(f"{label}指令：text={text!r} 识别到 @用户 {at_ids}")

        # 解析操作与目标 ID
        action = "add"
        rest = text
        for kw in ("移除", "删除"):
            if rest.startswith(kw):
                action = "remove"
                rest = rest[len(kw):].strip()
                break
        if rest.startswith("查看") or rest.startswith("列表"):
            action = "list"
            rest = ""
        ids = list(at_ids)
        for token in rest.replace("，", " ").replace(",", " ").split():
            token = token.strip()
            if not token:
                continue
            # 适配器会把 @昵称 拼进消息文本（如 "@做你的港湾"），不是 ID，跳过
            if token.startswith("@"):
                continue
            # 形如 昵称(123456) 的文本：提取括号内的数字 ID
            m = re.search(r"\((\d+)\)\s*$", token)
            if m:
                token = m.group(1)
            if self._is_valid_user_id(token) and token not in ids:
                ids.append(token)

        current = self._ids(self.config.get(key))

        # 自动清理历史混入的昵称等无效条目（如 "@做你的港湾"、"受你一靠子(534889516)"）
        cleaned = [uid for uid in current if self._is_valid_user_id(uid)]
        if len(cleaned) != len(current):
            current = cleaned
            self.config[key] = current
            self._save_config()
            logger.info(f"已清理{label}中的无效条目: {current}")

        if action == "list" or (action == "add" and not ids):
            if action == "add" and not ids:
                # 没有 @ 也没有填 ID：明确提示
                await event.send(
                    MessageChain(
                        [
                            Plain(
                                f"⚠️ 没有识别到 @ 的用户或用户ID。\n"
                                f"💡 用法：/{label} @用户或ID 添加；/{label} 移除@用户 删除。"
                            )
                        ]
                    )
                )
                return
            # 展示当前列表
            if current:
                lines = [f"📋 当前{label}（{len(current)} 个）："]
                lines += [f"{i + 1}. {uid}" for i, uid in enumerate(current)]
            else:
                lines = [f"📋 当前{label}为空。"]
            lines.append(f"💡 用法：/{label} @用户或ID 添加；/{label} 移除@用户 删除。")
            await event.send(MessageChain([Plain("\n".join(lines))]))
            return

        changed = False
        if action == "remove":
            for uid in ids:
                if uid in current:
                    current.remove(uid)
                    changed = True
        else:
            for uid in ids:
                if uid not in current:
                    current.append(uid)
                    changed = True

        if not changed:
            state = "已在" if action == "add" else "不在"
            await event.send(
                MessageChain(
                    [
                        Plain(
                            f"⚠️ {'、'.join(ids)} {state}{label}中，无需操作。\n"
                            f"当前{label}：{'、'.join(current) or '空'}"
                        )
                    ]
                )
            )
            return

        self.config[key] = current
        self._save_config()
        verb = f"已从{label}移除" if action == "remove" else f"已加入{label}"
        logger.info(f"{label}变更：{action} {ids} -> {current}")
        await event.send(
            MessageChain(
                [
                    Plain(
                        f"⚙️✅ {verb}：{'、'.join(ids)}\n"
                        f"当前{label}（{len(current)} 个）：{'、'.join(current) or '空'}"
                    )
                ]
            )
        )
