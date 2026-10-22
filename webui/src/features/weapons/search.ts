import type { WeaponEntry } from '../assets/types'

const PINYIN_COLLATOR = new Intl.Collator('zh-Hans-CN-u-co-pinyin')
const INITIAL_ANCHORS = Array.from('阿八嚓搭蛾发旮哈击喀垃妈拿噢啪期然撒塌挖夕压匝')
const INITIAL_LETTERS = 'abcdefghjklmnopqrstwxyz'

export const weaponPinyinInitials = (name: string): string => Array.from(name).map(character => {
  if (/[a-z0-9]/i.test(character)) return character.toLowerCase()
  if (!/[\u3400-\u9fff]/.test(character)) return ''
  let index = INITIAL_ANCHORS.length - 1
  while (index > 0 && PINYIN_COLLATOR.compare(character, INITIAL_ANCHORS[index]) < 0) index -= 1
  return INITIAL_LETTERS[index]
}).join('')

export const matchesWeaponQuery = (entry: WeaponEntry, query: string): boolean => {
  const term = query.trim().toLocaleLowerCase()
  if (!term) return true
  const searchable = [
    entry.name ?? '',
    entry.weapon_id,
    String(entry.rarity),
    String(entry.weapon_type),
    entry.model_status,
    entry.name_status,
    weaponPinyinInitials(entry.name ?? ''),
    ...entry.variants.flatMap(variant => [variant.variant, variant.semantic_status, variant.item_id, variant.resource_path]),
  ]
  return searchable.some(value => value.toLocaleLowerCase().includes(term))
}
