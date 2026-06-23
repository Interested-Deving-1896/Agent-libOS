from __future__ import annotations

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.images.default_agents.base import build_base_agent_image
from agent_libos.images.default_agents.coding import build_coding_agent_image
from agent_libos.images.default_agents.context_compressor import build_context_compressor_image
from agent_libos.images.default_agents.review import build_review_agent_image
from agent_libos.images.default_agents.toolmaker import build_toolmaker_agent_image
from agent_libos.models import AgentImage


def build_default_images(config: AgentLibOSConfig = DEFAULT_CONFIG) -> dict[str, AgentImage]:
    images = [
        build_base_agent_image(config),
        build_coding_agent_image(config),
        build_toolmaker_agent_image(),
        build_review_agent_image(),
        build_context_compressor_image(),
    ]
    return {image.image_id: image for image in images}


DEFAULT_IMAGES: dict[str, AgentImage] = build_default_images()
