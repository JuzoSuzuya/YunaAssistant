"""Заглушка minerl.herobraine.hero.mc — нужна только чтобы lib/actions.py импортировался
без установки полного MineRL (Malmo/Java/огромный пакет). MINERL_ITEM_MAP используется
только в equip/craft-путях действий, которых нет в нашем action space (TARGET_ACTION_SPACE
в agent.py не содержит "equip"/"craft"), так что реальным содержимым словаря можно не
заниматься — до него инференс никогда не доходит.
"""
MINERL_ITEM_MAP: dict[str, int] = {}
