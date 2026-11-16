"""asset_studio policy 业务域。"""
from __future__ import annotations
from typing import Literal


# 每种操作要模型输出哪几段、落到表单的哪个字段。
# 段名写进提示词、也用它切结果——两头同一份表，改一处不会漏改另一处。
OUTPUT_SPEC: dict[str, list[tuple[str, str]]] = {
    "character_profile": [
        ("【履历】", "biography"),
        ("【性格】", "personality"),
        ("【成长过程】", "growth_arc"),
        ("【一致性提示词】", "consistency_prompt"),
    ],
    "scene_setting": [
        ("【说明】", "summary"),
        ("【一致性提示词】", "consistency_prompt"),
    ],
    "prop_setting": [
        ("【说明】", "summary"),
        ("【一致性提示词】", "consistency_prompt"),
    ],
}


# 每种操作对"这段该写什么"的提示，来自开发说明 §4.2／§4.3／§4.4。
# 写在这里而不是让模型自由发挥：一致性提示词的口径要和详情页那段说明对得上，
# 用户在两处看到的应该是同一句话。
GUIDANCE: dict[str, str] = {
    "character_profile": "面部与识别特征、发型、服装、体型与总体风格、必须保持不变的细节",
    "scene_setting": "空间结构、建筑与地形、时间与天气、光线、色彩、环境材质、机位与风格约束",
    "prop_setting": "轮廓、比例、材质、颜色、磨损与纹理、关键识别特征、使用状态",
}


# 文字任务模式（增强包开发说明之外、本轮任务书 §5.4 点名的那件事）。
# 为什么要它：只有"生成完整设定"一种模式时，每一次点击都把已有的三段重写成
# 三份新的小作文——而人真正想要的往往只是"把空的填上"或"这段再展开一层"。
# 更麻烦的是它会顺带把已经定稿的段落改掉，等于让模型第二次写同一格。
# 模式决定两件事：**要模型输出哪几段**（`sections_for`）和**给它什么指令**
# （`MODE_INSTRUCTIONS`），两者都在服务端算，界面上只选不判。
TEXT_MODES: dict[str, str] = {
    "full": "生成完整设定",
    "fill_missing": "只补空着的字段",
    "polish": "优化已有设定",
    "expand": "扩写指定字段",
    "from_source": "依据原文片段写设定",
}


MODE_INSTRUCTIONS: dict[str, str] = {
    "full": "按现有信息把下面这几段写完整；已有内容可以对得上，但不要照抄成三份重复的话。",
    "fill_missing": "下面列出的段落现在是空的，**只写这些**。已有的段落不要重写、"
                    "不要在回复里复述一遍。",
    "polish": "这几段已经有内容了。只做文字层面的收紧与具体化：**不新增事实**"
              "（不加原文里没有的经历、关系、能力），不改人名与已经定下的设定。",
    "expand": "把指定的这一段往深处展开一层：写法、动机、后果都可以补，"
              "但补出来的必须和已有内容不矛盾，且不改动其他段落。",
    "from_source": "只依据最后给出的原文片段来写。原文没有的**不要编**，"
                   "确实要推测的地方在句子里标「建议」。",
}

# `from_source` 这一种要额外输入（原文片段），服务端先判——别等人勾了云端确认、
# 钱已经花出去了，才发现少填了一样东西。
MODES_NEED_EXCERPT = ("from_source",)


# 资产类型 → 允许的形态类型。DDL 里的 CHECK 只能收死取值范围，
# "outfit 只能挂在人物下"这种跨表规则 SQLite 写不了（CHECK 里不能引用别的表），
# 所以在这层判——报错也要报得比"违反约束"像人话。
VARIANT_TYPES_BY_KIND: dict[str, tuple[str, ...]] = {
    "character": ("outfit", "other"),
    "scene": ("scene_view", "other"),
    "prop": ("prop_form", "other"),
}


TEXT_CAPABILITY: Literal["text"] = "text"

IMAGE_CAPABILITY: Literal["image"] = "image"

# 图像那一支的操作名：文字设定有三种（人物／场景／道具），图像只有这一种。
# 它同时是"这条作业没有分段回稿"的判据——见 generation_view 里那一支。
IMAGE_OPERATION = "asset_image"



class AssetGenInvalid(Exception):
    """提交前的校验没过（操作与类型不匹配、提示词空、云端没确认…）。路由转 422。"""



class ProfileInvalid(Exception):
    """给非人物资产读写档案。转 422，不是 404——资产在，只是这类资产没有档案这一层。"""



class VariantMissing(Exception):
    """形态不存在，或它所属的资产对这个人不可见。转 404。"""



class VariantConflict(Exception):
    """同一资产同类型下重名。转 409，让人改名，不自动加序号。"""


class ImportUnusable(Exception):
    """那条作业不能用来导入资产图（不成功／不是图像／不可见）。转 409。"""



class ImportArtifactBad(Exception):
    """产物序号越界或文件已经不在了。转 422。"""



class ImportImageRejected(Exception):
    """产物不是允许的图片格式或超限。转 415。"""
