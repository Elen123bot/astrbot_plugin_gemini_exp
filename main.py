from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import *
from astrbot.api.message_components import *
import asyncio
import sys
import importlib
import requests
from io import BytesIO
import time
import tempfile
import os
from google import genai  # 修改为正确的导入方式
from google.genai import types
from PIL import Image as PILImage
from google.genai.types import HttpOptions
from astrbot.core.utils.io import download_image_by_url



@register("astrbot_plugin_geminiexp", "YourName", "基于 Google Gemini 2.0 Flash Experimental 多模态模型的插件", "1.0.0")
class GeminiExpPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.api_key = config.get("api_key", "")
        self.base_url = config.get("base_url", "https://generativelanguage.googleapis.com")
        self.command_aliases = config.get("command_aliases", ["gem", "edit"])
        self.model = config.get("model", "gemini-2.0-flash-exp")
        self.waiting_users = {}  # 存储正在等待输入的用户 {user_id: expiry_time}
        self.last_command_time = {}  # 存储最后命令的时间戳
        self.temp_dir = tempfile.mkdtemp(prefix="gemini_exp_")
        
        # 检查并安装必要的包
        if not self._check_packages():
            self._install_packages()
        

        
    def _check_packages(self) -> bool:
        """检查是否安装了需要的包"""
        try:
            importlib.import_module('google')
            importlib.import_module('PIL')
            return True
        except ImportError:
            return False

    def _install_packages(self):
        """安装必要的包"""
        try:
            import subprocess
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-U", "google-genai", "pillow"])
            print("成功安装必要的包")
        except subprocess.CalledProcessError as e:
            print(f"安装包失败: {str(e)}")
            raise
        
    @filter.command("gemexp", alias=[])
    async def gemini_exp(self, event: AstrMessageEvent):
        '''使用Gemini 2.0 Flash Experimental模型进行多模态交互'''
        # 检查API密钥是否配置
        if not self.api_key:
            yield event.plain_result("请联系管理员配置Gemini API密钥")
            return
        
        # 获取用户ID
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        
        # 记录命令的时间戳
        self.last_command_time[user_id] = time.time()
        
        # 设置等待状态，有效期60秒
        self.waiting_users[user_id] = time.time() + 60
        
        # 发送提示消息，然后返回，而不是继续处理
        yield event.plain_result(f"好的 {user_name}，请在60秒内发送您的文本描述和图片（如有）")
        return
    
    @filter.event_message_type(EventMessageType.ALL)
    async def handle_follow_up(self, event: AstrMessageEvent):
        """处理用户的后续消息"""
        
        # 确保这是一个消息事件
        if not isinstance(event, AstrMessageEvent):
            logger.error(f"handle_follow_up收到了错误类型的参数: {type(event)}")
            return
        
        # 获取用户ID
        user_id = event.get_sender_id()
        
        # 忽略命令消息
        message_text = event.message_str.strip()
        if message_text.startswith("/"):
            logger.debug(f"忽略命令消息: {message_text}")
            return
            
        # 严格检查 - 忽略包含触发词的消息
        # 这是为了避免将命令本身当作用户输入
        if "gemexp" in message_text.lower() or any(alias in message_text.lower() for alias in self.command_aliases):
            logger.debug(f"忽略包含触发词的消息: {message_text}")
            return
        
        # 检查用户是否在等待状态
        if user_id not in self.waiting_users:
            logger.debug(f"用户 {user_id} 不在等待状态")
            return
            
        # 时间戳检查 - 确保消息是在命令之后一段时间发送的
        # 这是为了避免将命令前后的消息当作用户输入
        current_time = time.time()
        if user_id in self.last_command_time:
            cmd_time = self.last_command_time[user_id]
            # 忽略太接近命令时间的消息 (1秒内)
            if current_time - cmd_time < 1.0:
                logger.debug(f"忽略太接近命令时间的消息，时间差: {current_time - cmd_time}秒")
                return
        
        # 检查等待是否过期
        if current_time > self.waiting_users[user_id]:
            del self.waiting_users[user_id]
            if user_id in self.last_command_time:
                del self.last_command_time[user_id]
            logger.info(f"用户 {user_id} 等待超时")
            yield event.plain_result("等待超时，请重新发送命令。")
            return
        
        # 打印更多调试信息
        logger.info(f"收到用户 {user_id} 的后续消息: {message_text[:30]}...")
        
        # 移除用户的等待状态，确保不会重复处理
        expiry_time = self.waiting_users.pop(user_id, None)
        if expiry_time is None:
            logger.warning(f"用户 {user_id} 状态已被其他进程处理")
            return
        
        # 清除命令时间记录
        if user_id in self.last_command_time:
            del self.last_command_time[user_id]
        
        logger.info(f"开始处理用户 {user_id} 的后续消息")
        
        # 获取消息内容
        message_chain = event.get_messages()
        text_content = event.message_str
        image_list = []
        
        # 从消息链中提取图片
        for msg in message_chain:
            if isinstance(msg, Image):
                try:
                    # 获取图片URL
                    img_url = None
                    if hasattr(msg, 'url') and msg.url:
                        img_url = msg.url
                    
                    if img_url:
                        # 使用框架提供的下载函数
                        temp_img_path = await download_image_by_url(img_url)
                        
                        # 使用PIL打开图片
                        img = PILImage.open(temp_img_path)
                        image_list.append(img)
                        logger.info(f"成功下载图片: {img_url}")
                except Exception as e:
                    logger.error(f"处理图片时出错: {str(e)}")
                    yield event.plain_result(f"无法处理图片，请稍后再试或尝试其他图片。错误: {str(e)}")
                    return
        
        if not text_content and not image_list:
            logger.info(f"用户 {user_id} 没有提供任何内容")
            yield event.plain_result("请提供文本描述或图片。")
            return
        
        # 发送处理中的消息
        logger.info(f"开始处理用户 {user_id} 的请求")
        yield event.plain_result("正在处理您的请求，请稍候...")
        
        # 调用Gemini API
        try:
            result = await self.process_with_gemini(text_content, image_list)
            text_response = result.get('text', '无文本回复')
            image_paths = result.get('image_paths', [])
            
            # 如果图片数量小于2，使用普通消息链发送
            if len(image_paths) < 2:
                # 构建回复消息链
                chain = [Plain(text_response)]
                
                # 添加图片到消息链
                for img_path in image_paths:
                    chain.append(Image.fromFileSystem(img_path))
                
                yield event.chain_result(chain)
            else:
                # 如果有2张或更多图片，使用群合并转发消息
                bot_id = self.config.get("bot_id", 114514)  # 使用配置或默认值
                bot_name = self.config.get("bot_name", "Gemini助手")  # 使用配置或默认值
                
                # 尝试将文本分割成与图片数量相等的部分
                text_parts = []
                
                # 尝试按段落分割文本（通过双换行符）
                paragraphs = text_response.split('\n\n')
                if len(paragraphs) >= len(image_paths):
                    # 如果段落数量足够，将它们分组为与图片数量相同的部分
                    for i in range(len(image_paths)):
                        if i < len(image_paths) - 1:
                            # 前面的图片取对应段落
                            parts_per_section = len(paragraphs) // len(image_paths)
                            start_idx = i * parts_per_section
                            end_idx = (i + 1) * parts_per_section
                            section_text = '\n\n'.join(paragraphs[start_idx:end_idx])
                            text_parts.append(section_text)
                        else:
                            # 最后一张图片取剩余所有段落
                            section_text = '\n\n'.join(paragraphs[i * (len(paragraphs) // len(image_paths)):])
                            text_parts.append(section_text)
                else:
                    # 如果段落不够，简单平均分配文本
                    total_chars = len(text_response)
                    chars_per_image = total_chars // len(image_paths)
                    
                    for i in range(len(image_paths)):
                        if i < len(image_paths) - 1:
                            # 前面的图片每个分配等量字符
                            start_idx = i * chars_per_image
                            end_idx = (i + 1) * chars_per_image
                            # 尝试在句子边界分割
                            j = end_idx
                            while j < min(total_chars, end_idx + 50) and j < total_chars:
                                if text_response[j] in ['.', '?', '!', '。', '？', '！']:
                                    end_idx = j + 1
                                    break
                                j += 1
                            text_parts.append(text_response[start_idx:end_idx])
                        else:
                            # 最后一张图片取剩余所有文本
                            text_parts.append(text_response[i * chars_per_image:])
                
                # 确保文本部分与图片数量一致
                if len(text_parts) < len(image_paths):
                    # 如果文本部分不够，填充空字符串
                    text_parts.extend([''] * (len(image_paths) - len(text_parts)))
                elif len(text_parts) > len(image_paths):
                    # 如果文本部分太多，合并多余部分到最后一个
                    text_parts = text_parts[:len(image_paths)-1] + ['\n\n'.join(text_parts[len(image_paths)-1:])]
                
                # 创建Nodes对象
                ns = Nodes([])
                
                # 向Nodes添加Node对象
                for i, (text_part, img_path) in enumerate(zip(text_parts, image_paths)):
                    # 添加适当的前缀
                    prefix = f"图片 {i+1}/{len(image_paths)}\n\n" if i > 0 else ""
                    
                    # 创建消息链
                    chain = [
                        Plain(prefix + text_part),
                        Image.fromFileSystem(img_path)
                    ]
                    
                    # 创建Node并添加到Nodes中
                    ns.nodes.append(
                        Node(
                            uin=bot_id,
                            name=bot_name,
                            content=chain
                        )
                    )
                
                # 将Nodes对象包装在列表中发送
                yield event.chain_result([ns])
            
        except Exception as e:
            logger.error(f"Gemini API调用失败: {str(e)}")
            yield event.plain_result(f"处理失败: {str(e)}")

    
    async def process_with_gemini(self, text, images):
        """处理图片和文本，调用Gemini API"""
        try:
            # 配置自定义 base_url
            http_options = HttpOptions(
                base_url=self.base_url
            )

            # 初始化客户端
            client = genai.Client(
                api_key=self.api_key,
                http_options=http_options
            )
            
            # 准备内容
            contents = []
            if text:
                contents.append(text)
            
            # 添加图片
            for img in images:
                contents.append(img)
            
            # 将内容转换为请求格式
            if len(contents) == 2 and text and len(images) == 1:
                # 如果是单文本+单图片，按照参考代码的格式处理
                contents = (text, images[0])
            
            if not contents:
                raise ValueError("没有有效的内容可以发送给Gemini API")
            
            # 调用API
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(response_modalities=['Text', 'Image'])
            )
            
            # 添加错误处理和日志记录
            logger.info(f"Gemini API响应: {response}")
            
            # 解析响应
            result = {'text': '', 'image_paths': []}
            
            # 添加空值检查
            if not response:
                raise ValueError("Gemini API返回了空响应")
                
            if not hasattr(response, 'candidates') or not response.candidates:
                raise ValueError("Gemini API返回的candidates为空")
                
            if not hasattr(response.candidates[0], 'content') or not response.candidates[0].content:
                raise ValueError("Gemini API返回的content为空")
                
            if not hasattr(response.candidates[0].content, 'parts') or not response.candidates[0].content.parts:
                raise ValueError("Gemini API返回的parts为空")
            
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'text') and part.text is not None:
                    result['text'] += part.text
                elif hasattr(part, 'inline_data') and part.inline_data is not None:
                    # 将图片数据保存为临时文件
                    img_data = part.inline_data.data
                    img = PILImage.open(BytesIO(img_data))
                    
                    # 创建临时文件路径
                    temp_file_path = os.path.join(self.temp_dir, f"gemini_result_{time.time()}.png")
                    
                    # 保存图片
                    img.save(temp_file_path, format="PNG")
                    
                    # 添加图片路径到结果
                    result['image_paths'].append(temp_file_path)
            
            return result
            
        except Exception as e:
            logger.error(f"Gemini API处理失败: {str(e)}")
            raise e

    async def terminate(self):
        '''插件被卸载/停用时调用'''
        self.waiting_users.clear()
        
        # 清理临时文件
        if os.path.exists(self.temp_dir):
            try:
                for file in os.listdir(self.temp_dir):
                    os.remove(os.path.join(self.temp_dir, file))
                os.rmdir(self.temp_dir)
            except Exception as e:
                logger.error(f"清理临时文件时出错: {str(e)}")

    # 重写initialize方法动态设置别名
    async def initialize(self):
        '''插件初始化时调用'''
        # 获取命令处理方法
        method = getattr(self, 'gemini_exp')
        
        # 获取filter属性
        if hasattr(method, '__astrbot_filter__'):
            # 更新别名
            method.__astrbot_filter__.alias = self.command_aliases
            logger.info(f"Gemini插件已设置命令别名: {self.command_aliases}")
