import tiktoken
from llmrankers.rankers import LlmRanker, SearchResult
from typing import List
import copy
import openai
import torch
from transformers import T5Tokenizer, T5ForConditionalGeneration, AutoTokenizer, AutoModelForCausalLM, AutoConfig


def max_tokens(model):
    if 'gpt-4' in model:
        return 8192
    else:
        return 4096


def get_post_prompt(query, num):
    # 返回排序任务的结尾指令，引导 GPT 只输出 passage 的排序结果。
    return f"Search Query: {query}. \nRank the {num} passages above based on their relevance to the search query. The passages should be listed in descending order using identifiers. The most relevant passages should be listed first. The output format should be [] > [], e.g., [1] > [2]. Only response the ranking results, do not say any word or explain."


def get_prefix_prompt(query, num):
    # 生成用于多轮对话形式调用 GPT 的“前缀 prompt”，建立排序任务上下文。
    return [{'role': 'system',
             'content': "You are RankGPT, an intelligent assistant that can rank passages based on their relevancy to the query."},
            {'role': 'user',
             'content': f"I will provide you with {num} passages, each indicated by number identifier []. \nRank the passages based on their relevance to query: {query}."},
            {'role': 'assistant', 'content': 'Okay, please provide the passages.'}]

# 根据messages和model计算token的数量


def num_tokens_from_messages(messages, model="gpt-3.5-turbo-0301"):
    """Returns the number of tokens used by a list of messages."""
    if model == "gpt-3.5-turbo":
        return num_tokens_from_messages(messages, model="gpt-3.5-turbo-0301")
    elif model == "gpt-4":
        return num_tokens_from_messages(messages, model="gpt-4-0314")
    elif model == "gpt-3.5-turbo-0301":
        # every message follows <|start|>{role/name}\n{content}<|end|>\n
        tokens_per_message = 4
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif model == "gpt-4-0314":
        tokens_per_message = 3
        tokens_per_name = 1
    else:
        tokens_per_message, tokens_per_name = 0, 0

    try:
        encoding = tiktoken.get_encoding(model)
    except:
        encoding = tiktoken.get_encoding("cl100k_base")

    num_tokens = 0
    if isinstance(messages, list):
        for message in messages:
            num_tokens += tokens_per_message
            for key, value in message.items():
                num_tokens += len(encoding.encode(value))
                if key == "name":
                    num_tokens += tokens_per_name
    else:
        num_tokens += len(encoding.encode(messages))
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens

# 生成一个包含查询和文档的提示文本，适用于使用聊天模型（例如 GPT）进行排序的情况。


def create_permutation_instruction_chat(query: str, docs: List[SearchResult], model_name='gpt-3.5-turbo'):
    # 生成排列任务聊天内容
    num = len(docs)

    max_length = 300
    while True:
        messages = get_prefix_prompt(query, num)
        rank = 0
        for doc in docs:
            rank += 1
            content = doc.text
            # 去掉开头
            content = content.replace('Title: Content: ', '')
            # 去除 content 字符串的前后空白字符
            content = content.strip()
            # For Japanese should cut by character: content = content[:int(max_length)]
            # 按空格分割，取前max_length个单词
            content = ' '.join(content.split()[:int(max_length)])
            messages.append({'role': 'user', 'content': f"[{rank}] {content}"})
            messages.append(
                {'role': 'assistant', 'content': f'Received passage [{rank}].'})
        messages.append(
            {'role': 'user', 'content': get_post_prompt(query, num)})

        if model_name is not None:
            # 直到它不超过最大tokens-200再退出循环，否则一直减小长度
            if num_tokens_from_messages(messages, model_name) <= max_tokens(model_name) - 200:
                break
            else:
                max_length -= 1
        else:
            break
    return messages

# 适用于其他模型（如 T5）进行排序时，生成的提示格式稍有不同。


def create_permutation_instruction_complete(query: str, docs: List[SearchResult]):
    num = len(docs)
    message = f"This is RankGPT, an intelligent assistant that can rank passages based on their relevancy to the query.\n\n" \
              f"The following are {num} passages, each indicated by number identifier []. " \
              f"I can rank them based on their relevance to query: {query}\n\n"

    rank = 0
    for doc in docs:
        rank += 1
        content = doc.text
        content = content.replace('Title: Content: ', '')
        content = content.strip()
        content = ' '.join(content.split()[:300])
        message += f"[{rank}] {content}\n\n"
    message += f"The search query is: {query}"
    message += f"I will rank the {num} passages above based on their relevance to the search query. The passages " \
               "will be listed in descending order using identifiers, and the most relevant passages should be listed "\
               "first, and the output format should be [] > [] > etc, e.g., [1] > [2] > etc.\n\n" \
               f"The ranking results of the {num} passages (only identifiers) is:"
    return message


def clean_response(response: str):
    # 去除响应中的非数字，并将它们以空格分隔。
    new_response = ''
    for c in response:
        if not c.isdigit():
            new_response += ' '
        else:
            new_response += c
    new_response = new_response.strip()
    return new_response


def remove_duplicate(response):
    # 删除响应中的重复项。
    new_response = []
    for c in response:
        if c not in new_response:
            new_response.append(c)
    return new_response

# 这个函数用于处理模型返回的排序响应，将其与当前排名的文档进行比较，并根据返回的排序调整文档顺序。


def receive_permutation(ranking, permutation, rank_start=0, rank_end=100):
    # 根据排列，对ranking中start到end的部分进行排序
    response = clean_response(permutation)
    # 多个空格不会影响，会被一起跳过
    response = [int(x) - 1 for x in response.split()]
    response = remove_duplicate(response)
    # 从 ranking 中提取从 rank_start 到 rank_end 之间的部分 0-99
    cut_range = copy.deepcopy(ranking[rank_start: rank_end])
    original_rank = [tt for tt in range(len(cut_range))]
    response = [ss for ss in response if ss in original_rank]
    response = response + [tt for tt in original_rank if tt not in response]

    for j, x in enumerate(response):
        ranking[j + rank_start] = cut_range[x]

    return ranking


class OpenAiListwiseLlmRanker(LlmRanker):
    def __init__(self, model_name_or_path, api_key, window_size, step_size, num_repeat):
        self.llm = model_name_or_path
        self.tokenizer = tiktoken.encoding_for_model(model_name_or_path)
        self.window_size = window_size
        self.step_size = step_size
        self.num_repeat = num_repeat
        openai.api_key = api_key
        self.total_compare = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
    # 接受一个查询和一组文档，并返回排序的结果。具体的排序操作由调用 OpenAI API 或其他模型来完成。

    def compare(self, query: str, docs: List):
        self.total_compare += 1
        messages = create_permutation_instruction_chat(query, docs, self.llm)
        while True:
            try:
                completion = openai.ChatCompletion.create(
                    model=self.llm,
                    messages=messages,
                    temperature=0.0,
                    request_timeout=15)
                self.total_completion_tokens += int(
                    completion['usage']['completion_tokens'])
                self.total_prompt_tokens += int(
                    completion['usage']['prompt_tokens'])
                return completion['choices'][0]['message']['content']
            except Exception as e:
                print(str(e))
                if "This model's maximum context length is" in str(e):
                    print('reduce_length')
                    return 'ERROR::reduce_length'

    def rerank(self,  query: str, ranking: List[SearchResult]) -> List[SearchResult]:
        self.total_compare = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

        for _ in range(self.num_repeat):
            ranking = copy.deepcopy(ranking)
            end_pos = len(ranking)
            start_pos = end_pos - self.window_size
            ############################## TODO ############################
            #
            # 提示：使用self.compare和receive_permutation函数进行实现
            # 排序过程重复的次数
            while start_pos >= 0:
                # 从 ranking 中提取出当前窗口的文档
                window_docs = ranking[start_pos:start_pos + self.window_size]

                # 使用 compare 函数来比较这个窗口内的文档
                permutation = self.compare(query, window_docs)

                # 根据 compare 的结果更新当前排名
                ranking = receive_permutation(
                    ranking, permutation, start_pos, start_pos + self.window_size)

                # 更新窗口的位置
                start_pos -= self.step_size
            #
            ################################################################

        for i, doc in enumerate(ranking):
            doc.score = -i
        return ranking

    def truncate(self, text, length):
        return self.tokenizer.decode(self.tokenizer.encode(text)[:length])


class ListwiseLlmRanker(OpenAiListwiseLlmRanker):
    CHARACTERS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L",
                  "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V",
                  "W"]  # "Passage X" and "Passage Y" will be tokenized into 3 tokens, so we dont use for now

    def __init__(self, model_name_or_path, tokenizer_name_or_path, device, window_size, step_size,
                 scoring='generation', num_repeat=1, cache_dir=None):

        self.scoring = scoring
        self.device = device
        self.window_size = window_size
        self.step_size = step_size
        self.num_repeat = num_repeat
        self.config = AutoConfig.from_pretrained(
            model_name_or_path, cache_dir=cache_dir)

        if self.config.model_type == 't5':
            self.tokenizer = T5Tokenizer.from_pretrained(tokenizer_name_or_path
                                                         if tokenizer_name_or_path is not None else
                                                         model_name_or_path, cache_dir=cache_dir)
            self.llm = T5ForConditionalGeneration.from_pretrained(model_name_or_path,
                                                                  device_map='auto',
                                                                  torch_dtype=torch.float16 if device == 'cuda'
                                                                  else torch.float32,
                                                                  cache_dir=cache_dir)

            self.decoder_input_ids = self.tokenizer.encode("<pad> Passage",
                                                           return_tensors="pt",
                                                           add_special_tokens=False).to(self.device) if self.tokenizer else None
            self.target_token_ids = self.tokenizer.batch_encode_plus([f'<pad> Passage {self.CHARACTERS[i]}'
                                                                      for i in range(len(self.CHARACTERS))],
                                                                     return_tensors="pt",
                                                                     add_special_tokens=False,
                                                                     padding=True).input_ids[:, -1]
        elif self.config.model_type == 'llama':
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name_or_path, cache_dir=cache_dir)
            self.tokenizer.use_default_system_prompt = False
            if 'vicuna' and 'v1.5' in model_name_or_path:
                self.tokenizer.chat_template = "{% if messages[0]['role'] == 'system' %}{% set loop_messages = messages[1:] %}{% set system_message = messages[0]['content'] %}{% else %}{% set loop_messages = messages %}{% set system_message = 'A chat between a curious user and an artificial intelligence assistant. The assistant gives helpful, detailed, and polite answers to the user\\'s questions.' %}{% endif %}{% for message in loop_messages %}{% if (message['role'] == 'user') != (loop.index0 % 2 == 0) %}{{ raise_exception('Conversation roles must alternate user/assistant/user/assistant/...') }}{% endif %}{% if loop.index0 == 0 %}{{ system_message }}{% endif %}{% if message['role'] == 'user' %}{{ ' USER: ' + message['content'].strip() }}{% elif message['role'] == 'assistant' %}{{ ' ASSISTANT: ' + message['content'].strip() + eos_token }}{% endif %}{% endfor %}{% if add_generation_prompt %}{{ ' ASSISTANT:' }}{% endif %}"

            self.llm = AutoModelForCausalLM.from_pretrained(model_name_or_path,
                                                            device_map='auto',
                                                            torch_dtype=torch.float16 if device == 'cuda'
                                                            else torch.float32,
                                                            cache_dir=cache_dir).eval()
        else:
            raise NotImplementedError(
                f"Model type {self.config.model_type} is not supported yet for listwise :(")

    def compare(self, query: str, docs: List):
        self.total_compare += 1
        if self.scoring == 'generation':
            if self.config.model_type == 't5':
                input_text = create_permutation_instruction_complete(
                    query, docs)
                input_ids = self.tokenizer(
                    input_text, return_tensors="pt", truncation=True).input_ids.to(self.device)
                self.total_prompt_tokens += input_ids.shape[1]

                output_ids = self.llm.generate(input_ids)[0]
                self.total_completion_tokens += output_ids.shape[0]
                output = self.tokenizer.decode(output_ids,
                                               skip_special_tokens=True).strip()
            elif self.config.model_type == 'llama':
                input_text = create_permutation_instruction_chat(
                    query, docs, model_name=None)
                input_ids = self.tokenizer.apply_chat_template(input_text, return_tensors="pt",
                                                               add_generation_prompt=True).to(self.device)

                self.total_prompt_tokens += input_ids.shape[1]

                output_ids = self.llm.generate(input_ids)[0]
                self.total_completion_tokens += output_ids.shape[0]
                output = self.tokenizer.decode(output_ids[input_ids.shape[1]:],
                                               skip_special_tokens=True).strip()

        elif self.scoring == 'likelihood':
            passages = "\n\n".join(
                [f'Passage {self.CHARACTERS[i]}: "{doc.text}"' for i, doc in enumerate(docs)])
            input_text = f'Given a query "{query}", which of the following passages is the most relevant one to the query?\n\n' \
                         + passages + '\n\nOutput only the passage label of the most relevant passage:'

            input_ids = self.tokenizer(
                input_text, return_tensors="pt").input_ids.to(self.device)
            self.total_prompt_tokens += input_ids.shape[1]

            with torch.no_grad():
                logits = self.llm(
                    input_ids=input_ids, decoder_input_ids=self.decoder_input_ids).logits[0][-1]
                distributions = torch.softmax(logits, dim=0)
                scores = distributions[self.target_token_ids[:len(docs)]]
                ranked = sorted(zip(
                    [f"[{str(i+1)}]" for i in range(len(docs))], scores), key=lambda x: x[1], reverse=True)
                output = '>'.join(ranked[i][0] for i in range(len(ranked)))

        return output

    def truncate(self, text, length):
        return self.tokenizer.convert_tokens_to_string(self.tokenizer.tokenize(text)[:length])
