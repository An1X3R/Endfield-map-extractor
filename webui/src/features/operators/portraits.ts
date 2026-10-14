import portrait0 from './portraits/chr_0002_endminm.svg'
import portrait1 from './portraits/chr_0003_endminf.svg'
import portrait2 from './portraits/chr_0004_pelica.svg'
import portrait3 from './portraits/chr_0005_chen.svg'
import portrait4 from './portraits/chr_0006_wolfgd.svg'
import portrait5 from './portraits/chr_0007_ikut.svg'
import portrait6 from './portraits/chr_0009_azrila.svg'
import portrait7 from './portraits/chr_0011_seraph.svg'
import portrait8 from './portraits/chr_0012_avywen.svg'
import portrait9 from './portraits/chr_0013_aglina.svg'
import portrait10 from './portraits/chr_0014_aurora.svg'
import portrait11 from './portraits/chr_0015_lifeng.svg'
import portrait12 from './portraits/chr_0016_laevat.svg'
import portrait13 from './portraits/chr_0017_yvonne.svg'
import portrait14 from './portraits/chr_0018_dapan.svg'
import portrait15 from './portraits/chr_0019_karin.svg'
import portrait16 from './portraits/chr_0020_meurs.svg'
import portrait17 from './portraits/chr_0021_whiten.svg'
import portrait18 from './portraits/chr_0022_bounda.svg'
import portrait19 from './portraits/chr_0023_antal.svg'
import portrait20 from './portraits/chr_0024_deepfin.svg'
import portrait21 from './portraits/chr_0025_ardelia.svg'
import portrait22 from './portraits/chr_0026_lastrite.svg'
import portrait23 from './portraits/chr_0027_tangtang.svg'
import portrait24 from './portraits/chr_0028_wulfa.svg'
import portrait25 from './portraits/chr_0029_pograni.svg'
import portrait26 from './portraits/chr_0030_zhuangfy.svg'
import portrait27 from './portraits/chr_0031_mifu.svg'
import portrait28 from './portraits/chr_0032_lizhiyan.svg'
import portrait29 from './portraits/chr_0033_camille.svg'
import portrait30 from './portraits/chr_0034_typhoea.svg'
import portrait31 from './portraits/chr_0035_liino.svg'

export type OperatorPortrait = Readonly<{ characterId: string; name: string; url: string }>

export const OPERATOR_PORTRAITS: Readonly<Record<string, OperatorPortrait>> = {
  chr_0002_endminm: { characterId: 'chr_0002_endminm', name: '管理员', url: portrait0 },
  chr_0003_endminf: { characterId: 'chr_0003_endminf', name: '管理员', url: portrait1 },
  chr_0004_pelica: { characterId: 'chr_0004_pelica', name: '佩丽卡', url: portrait2 },
  chr_0005_chen: { characterId: 'chr_0005_chen', name: '陈千语', url: portrait3 },
  chr_0006_wolfgd: { characterId: 'chr_0006_wolfgd', name: '狼卫', url: portrait4 },
  chr_0007_ikut: { characterId: 'chr_0007_ikut', name: '弧光', url: portrait5 },
  chr_0009_azrila: { characterId: 'chr_0009_azrila', name: '余烬', url: portrait6 },
  chr_0011_seraph: { characterId: 'chr_0011_seraph', name: '赛希', url: portrait7 },
  chr_0012_avywen: { characterId: 'chr_0012_avywen', name: '艾维文娜', url: portrait8 },
  chr_0013_aglina: { characterId: 'chr_0013_aglina', name: '洁尔佩塔', url: portrait9 },
  chr_0014_aurora: { characterId: 'chr_0014_aurora', name: '昼雪', url: portrait10 },
  chr_0015_lifeng: { characterId: 'chr_0015_lifeng', name: '黎风', url: portrait11 },
  chr_0016_laevat: { characterId: 'chr_0016_laevat', name: '莱万汀', url: portrait12 },
  chr_0017_yvonne: { characterId: 'chr_0017_yvonne', name: '伊冯', url: portrait13 },
  chr_0018_dapan: { characterId: 'chr_0018_dapan', name: '大潘', url: portrait14 },
  chr_0019_karin: { characterId: 'chr_0019_karin', name: '秋栗', url: portrait15 },
  chr_0020_meurs: { characterId: 'chr_0020_meurs', name: '卡契尔', url: portrait16 },
  chr_0021_whiten: { characterId: 'chr_0021_whiten', name: '埃特拉', url: portrait17 },
  chr_0022_bounda: { characterId: 'chr_0022_bounda', name: '萤石', url: portrait18 },
  chr_0023_antal: { characterId: 'chr_0023_antal', name: '安塔尔', url: portrait19 },
  chr_0024_deepfin: { characterId: 'chr_0024_deepfin', name: '阿列什', url: portrait20 },
  chr_0025_ardelia: { characterId: 'chr_0025_ardelia', name: '艾尔黛拉', url: portrait21 },
  chr_0026_lastrite: { characterId: 'chr_0026_lastrite', name: '别礼', url: portrait22 },
  chr_0027_tangtang: { characterId: 'chr_0027_tangtang', name: '汤汤', url: portrait23 },
  chr_0028_wulfa: { characterId: 'chr_0028_wulfa', name: '洛茜', url: portrait24 },
  chr_0029_pograni: { characterId: 'chr_0029_pograni', name: '骏卫', url: portrait25 },
  chr_0030_zhuangfy: { characterId: 'chr_0030_zhuangfy', name: '庄方宜', url: portrait26 },
  chr_0031_mifu: { characterId: 'chr_0031_mifu', name: '弭弗', url: portrait27 },
  chr_0032_lizhiyan: { characterId: 'chr_0032_lizhiyan', name: '诀', url: portrait28 },
  chr_0033_camille: { characterId: 'chr_0033_camille', name: '卡缪', url: portrait29 },
  chr_0034_typhoea: { characterId: 'chr_0034_typhoea', name: '提弗洛斯', url: portrait30 },
  chr_0035_liino: { characterId: 'chr_0035_liino', name: '梨诺', url: portrait31 },
}

/** Match stable catalog identity; names and costume variants never select another character. */
export function getOperatorPortrait(characterId: string | null): OperatorPortrait | null {
  return characterId && Object.hasOwn(OPERATOR_PORTRAITS, characterId) ? OPERATOR_PORTRAITS[characterId] : null
}
