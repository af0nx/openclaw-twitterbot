#!/usr/bin/env python3
"""
OpenRouter Client - Twitter Bot Pipeline V2
Unified LLM routing with automatic failover
Supports tiered model selection: eco, auto, premium
"""

import logging
import re
from typing import Dict, Any, Optional, List
import os
import random
import time

import requests
from openai import OpenAI
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

# Load environment
load_dotenv('/dev/shm/.env')

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class OpenRouterClient:
    """
    Unified LLM client using OpenRouter with tiered model selection
    
    Tiers (aligned with SkinBetAI pipeline):
    - eco: Cheap, fast models for parsing/summarization (gemini-2.5-flash)
    - auto: Balanced models for content generation (deepseek-chat, claude-haiku)
    - premium: High-quality models for guardrails (claude-sonnet, grok-2)
    """
    
    def __init__(self):
        self.api_key = os.getenv('OPENROUTER_API_KEY')
        self.base_url = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
        
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY not set")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        
        # Tier model mapping
        self.models = {
            'eco': os.getenv('LLM_TIER_ECO', 'google/gemini-2.5-flash'),
            'auto': os.getenv('LLM_TIER_AUTO', 'deepseek/deepseek-chat'),
            'premium': os.getenv('LLM_TIER_PREMIUM', 'anthropic/claude-3.5-sonnet'),
            'vision': os.getenv('LLM_TIER_VISION', 'google/gemini-2.5-flash'),
        }
        
        # ── A/B Testing ──────────────────────────────────────────────
        # When enabled, the 'auto' and 'premium' tiers randomly pick from
        # a candidate pool instead of always using the same model.
        # Set AB_TEST_ENABLED=true in env to activate.
        self.ab_test_enabled = os.getenv('AB_TEST_ENABLED', 'false').lower() == 'true'
        
        # Direct OpenRouter client for models not available on ClawRouter
        self.direct_client = OpenAI(
            api_key=self.api_key,
            base_url='https://openrouter.ai/api/v1'
        )
        # Models that need direct OpenRouter (not in ClawRouter's model list)
        self._direct_models = {
            'z-ai/glm-5.1', 'minimax/minimax-m2.7', 'xiaomi/mimo-v2-pro',
        }
        # Reasoning models that need higher max_tokens (thinking eats tokens)
        self._reasoning_models = {
            'z-ai/glm-5.1', 'minimax/minimax-m2.7',
        }
        
        # Candidate pools for A/B testing — each tuple: (model_id, weight)
        # Higher weight = more traffic. Weights don't need to sum to 1.
        self.ab_pools = {
            'auto': [
                (os.getenv('LLM_TIER_AUTO', 'free/deepseek-v3.2'), 3),       # current baseline
                ('z-ai/glm-5.1', 2),                                          # zhipu GLM — strong chinese LLM
                ('minimax/minimax-m2.7', 2),                                   # minimax latest
                ('xiaomi/mimo-v2-pro', 2),                                     # xiaomi mimo
            ],
            'premium': [
                ('anthropic/claude-sonnet-4.6', 3),                            # best judgment
                ('z-ai/glm-5.1', 2),                                           # zhipu GLM
                ('minimax/minimax-m2.7', 2),                                   # minimax latest
                ('xiaomi/mimo-v2-pro', 1),                                     # xiaomi mimo
            ],
        }
        
        # Override pools from env: AB_POOL_AUTO="model1:3,model2:2"
        for tier_name in ('auto', 'premium'):
            env_pool = os.getenv(f'AB_POOL_{tier_name.upper()}', '')
            if env_pool:
                parsed = []
                for entry in env_pool.split(','):
                    entry = entry.strip()
                    if ':' in entry:
                        model_id, weight = entry.rsplit(':', 1)
                        parsed.append((model_id.strip(), int(weight)))
                    else:
                        parsed.append((entry, 1))
                if parsed:
                    self.ab_pools[tier_name] = parsed
        
        logger.info(f"✅ OpenRouter client initialized")
        logger.info(f"   eco: {self.models['eco']}")
        logger.info(f"   auto: {self.models['auto']}")
        logger.info(f"   premium: {self.models['premium']}")
        logger.info(f"   vision: {self.models['vision']}")
        if self.ab_test_enabled:
            for tier_name, pool in self.ab_pools.items():
                models_str = ', '.join(f"{m}(w={w})" for m, w in pool)
                logger.info(f"   🧪 A/B {tier_name}: {models_str}")
        else:
            logger.info(f"   A/B testing: OFF (set AB_TEST_ENABLED=true to activate)")
        
        # ── Daily budget cap ────────────────────────────────────────────
        # Falls back to free-tier models when daily spend exceeds cap.
        self._daily_budget = float(os.getenv('AB_DAILY_BUDGET', '0.40'))  # $0.40 default (~$12/month)
        self._budget_exceeded = False
        self._last_budget_check = 0.0  # epoch
        self._budget_check_interval = 300  # check every 5 min
        logger.info(f"   💰 Daily budget cap: ${self._daily_budget:.2f}")
    
    def _check_budget(self) -> bool:
        """Return True if daily spend is under budget. Caches result for 5 min."""
        now = time.time()
        if now - self._last_budget_check < self._budget_check_interval:
            return not self._budget_exceeded
        self._last_budget_check = now
        try:
            resp = requests.get(
                'https://openrouter.ai/api/v1/auth/key',
                headers={'Authorization': f'Bearer {self.api_key}'},
                timeout=5
            )
            data = resp.json().get('data', {})
            daily = data.get('usage_daily', 0)
            if daily >= self._daily_budget:
                if not self._budget_exceeded:
                    logger.warning(f"🚨 Daily budget exceeded: ${daily:.4f} >= ${self._daily_budget:.2f} — falling back to free models")
                self._budget_exceeded = True
                return False
            else:
                if self._budget_exceeded:
                    logger.info(f"✅ Budget reset: ${daily:.4f} < ${self._daily_budget:.2f}")
                self._budget_exceeded = False
                return True
        except Exception as e:
            logger.debug(f"Budget check failed (non-fatal): {e}")
            return not self._budget_exceeded  # keep last known state
    
    def _pick_model(self, tier: str) -> str:
        """Pick a model for the given tier — A/B rotates if enabled, else static.
        Falls back to free-tier baseline when daily budget is exceeded."""
        if self.ab_test_enabled and tier in self.ab_pools:
            if self._check_budget():
                pool = self.ab_pools[tier]
                models, weights = zip(*pool)
                chosen = random.choices(models, weights=weights, k=1)[0]
                return chosen
            else:
                # Budget exceeded — use free baseline only
                return self.models.get(tier, self.models['auto'])
        return self.models.get(tier, self.models['auto'])
    
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=4, max=120))
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        tier: str = 'auto',
        temperature: float = 0.7,
        max_tokens: int = 500,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate text using OpenRouter
        
        Args:
            prompt: User prompt
            system_prompt: System instructions
            tier: Model tier ('eco', 'auto', 'premium')
            temperature: Sampling temperature
            max_tokens: Max tokens to generate
            **kwargs: Additional OpenAI API parameters
        
        Returns:
            Dict with 'text', 'model', 'usage' keys
        """
        try:
            model = self._pick_model(tier)
            
            messages = []
            if system_prompt:
                messages.append({'role': 'system', 'content': system_prompt})
            messages.append({'role': 'user', 'content': prompt})
            
            # Use direct OpenRouter for models not in ClawRouter
            client = self.direct_client if model in self._direct_models else self.client
            
            # Reasoning models burn tokens on thinking — boost budget so
            # actual content isn't truncated.
            effective_max = max_tokens
            if model in self._reasoning_models and max_tokens < 2500:
                effective_max = 2500
                logger.debug(f"🧠 Boosted max_tokens {max_tokens}→{effective_max} for reasoning model {model}")
            
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=effective_max,
                **kwargs
            )
            
            raw_text = response.choices[0].message.content or ''
            # Strip reasoning <think>...</think> blocks from chain-of-thought models
            raw_text = re.sub(r'<think>.*?</think>\s*', '', raw_text, flags=re.DOTALL).strip()
            # Strip ClawRouter "Insufficient balance" warning prefix that pollutes content
            if raw_text.startswith('> **⚠️ Insufficient balance**'):
                lines = raw_text.split('\n', 2)
                # Skip the warning line and any blank line after it
                clean_lines = [l for l in lines[1:] if l.strip()]
                raw_text = '\n'.join(clean_lines).strip() if clean_lines else raw_text
                if raw_text != response.choices[0].message.content:
                    logger.warning(f"⚠️  Stripped ClawRouter balance warning from response (model: {response.model})")
            
            result = {
                'text': raw_text,
                'model': response.model,
                'usage': {
                    'prompt_tokens': response.usage.prompt_tokens,
                    'completion_tokens': response.usage.completion_tokens,
                    'total_tokens': response.usage.total_tokens
                },
                'finish_reason': response.choices[0].finish_reason
            }
            
            logger.debug(f"✅ Generated {result['usage']['completion_tokens']} tokens with {model}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ OpenRouter generation failed: {e}")
            raise
    
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=4, max=120))
    def generate_with_fallback(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        primary_tier: str = 'auto',
        fallback_tier: str = 'eco',
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate with automatic fallback to cheaper tier on failure
        
        This leverages OpenRouter's native failover internally,
        but also provides application-level fallback
        """
        try:
            return self.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                tier=primary_tier,
                **kwargs
            )
        except Exception as e:
            logger.warning(f"⚠️  Primary tier '{primary_tier}' failed, falling back to '{fallback_tier}'")
            return self.generate(
                prompt=prompt,
                system_prompt=system_prompt,
                tier=fallback_tier,
                **kwargs
            )
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=60))
    def generate_vision(
        self,
        prompt: str,
        image_path: str = None,
        image_base64: str = None,
        image_url: str = None,
        system_prompt: Optional[str] = None,
        tier: str = 'vision',
        temperature: float = 0.3,
        max_tokens: int = 1000,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate text from image + text prompt using multimodal LLM.
        Supports image via local file path, base64 string, or URL.
        Uses vision tier by default (Gemini Flash — fast, cheap, great vision).
        
        NOTE: Vision calls bypass ClawRouter and go directly to OpenRouter,
        because ClawRouter's free-tier fallback models don't support multimodal.
        """
        import base64 as b64_module
        
        model = self.models.get(tier, self.models['vision'])
        
        # Vision needs a direct OpenRouter client (ClawRouter free models can't do vision)
        vision_client = OpenAI(
            api_key=self.api_key,
            base_url='https://openrouter.ai/api/v1'
        )
        
        # Build image content part
        if image_path:
            with open(image_path, 'rb') as f:
                img_bytes = f.read()
            # Detect mime type
            if image_path.lower().endswith('.png'):
                mime = 'image/png'
            elif image_path.lower().endswith('.gif'):
                mime = 'image/gif'
            else:
                mime = 'image/jpeg'
            img_b64 = b64_module.b64encode(img_bytes).decode()
            image_content = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{img_b64}"}
            }
        elif image_base64:
            image_content = {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
            }
        elif image_url:
            image_content = {
                "type": "image_url",
                "image_url": {"url": image_url}
            }
        else:
            raise ValueError("Must provide image_path, image_base64, or image_url")
        
        messages = []
        if system_prompt:
            messages.append({'role': 'system', 'content': system_prompt})
        
        messages.append({
            'role': 'user',
            'content': [
                image_content,
                {"type": "text", "text": prompt}
            ]
        })
        
        try:
            response = vision_client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )
            
            raw_text = response.choices[0].message.content or ''
            if raw_text.startswith('> **⚠️ Insufficient balance**'):
                lines = raw_text.split('\n', 2)
                clean_lines = [l for l in lines[1:] if l.strip()]
                raw_text = '\n'.join(clean_lines).strip() if clean_lines else raw_text
            
            result = {
                'text': raw_text,
                'model': response.model,
                'usage': {
                    'prompt_tokens': response.usage.prompt_tokens,
                    'completion_tokens': response.usage.completion_tokens,
                    'total_tokens': response.usage.total_tokens
                },
                'finish_reason': response.choices[0].finish_reason
            }
            
            logger.debug(f"✅ Vision: {result['usage']['total_tokens']} tokens with {model}")
            return result
            
        except Exception as e:
            logger.error(f"❌ Vision generation failed: {e}")
            raise

    def generate_batch(
        self,
        prompts: List[str],
        system_prompt: Optional[str] = None,
        tier: str = 'auto',
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        Generate multiple completions
        Note: OpenRouter doesn't support native batch API yet,
        so this sends sequential requests
        """
        results = []
        for prompt in prompts:
            try:
                result = self.generate(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    tier=tier,
                    **kwargs
                )
                results.append(result)
            except Exception as e:
                logger.error(f"❌ Batch generation failed for prompt: {e}")
                results.append({
                    'text': '',
                    'error': str(e)
                })
        
        return results
    
    def parse_rss_content(self, content: str, category: str) -> Dict[str, Any]:
        """Parse RSS content to extract key points (eco tier)"""
        system_prompt = """You are a content parser for a betting/esports news bot.
Extract the most important information in 1-2 sentences.
Focus on: roster changes, match results, financial data, regulatory changes."""
        
        prompt = f"""Category: {category}
Content: {content}

Extract key information:"""
        
        return self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            tier='eco',
            temperature=0.3,
            max_tokens=150
        )
    
    def classify_urgency(self, headline: str, content: str) -> Dict[str, Any]:
        """Classify content urgency (eco tier)"""
        system_prompt = """You are an urgency classifier. Classify content as:
- breaking: Major news that needs immediate posting
- important: Significant news, post within hours
- normal: Regular news, schedule normally
- skip: Not relevant for our audience"""
        
        prompt = f"""Headline: {headline}
Content: {content}

Urgency classification (one word only):"""
        
        return self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            tier='eco',
            temperature=0.1,
            max_tokens=10
        )


# Singleton instance
_client = None

def get_openrouter_client() -> OpenRouterClient:
    """Get or create OpenRouter client singleton"""
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


if __name__ == '__main__':
    # Test the client
    client = get_openrouter_client()
    
    result = client.generate(
        prompt="Write a tweet about CS2 roster changes in a snarky tone. Max 280 chars.",
        tier='auto',
        temperature=0.8,
        max_tokens=100
    )
    
    print(f"\n✅ Test Result:")
    print(f"Model: {result['model']}")
    print(f"Text: {result['text']}")
    print(f"Tokens: {result['usage']['total_tokens']}")
