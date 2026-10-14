from .variety import VarietyAdapter
from .deadline import DeadlineAdapter
from .anime_corner import AnimeCornerAdapter
from .dramabeans import DramabeansAdapter

ADAPTERS = {a.key: a for a in (VarietyAdapter, DeadlineAdapter, AnimeCornerAdapter, DramabeansAdapter)}
