"""Check exact display relationships, discovery and explicit selections."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from endfield_character_catalog import CharacterIdentity, CharacterResourceProvider
from endfield_display_props import (
    DISPLAY_RESOURCE, RESOURCE_PREFIX, DisplayPropCatalog, DisplayPropError,
    assemble_display_prop_entries, display_prop_bindings, display_prop_resource_paths,
    public_display_prop_catalog, select_display_props,
)
from endfield_display_prop_export import _artifact_path


def configuration() -> dict[str, object]:
    return {'$animestudio':{'container':DISPLAY_RESOURCE},
        'data':{'_keys':['chr_future'], '_values':[{'rid':17}]},
        'references':{'RefIds':[{'rid':17,'type':{'class':'CharacterDisplayData',
            'ns':'Beyond.Gameplay','asm':'Gameplay.Beyond'},'data':{'exactTypeTreeDecoded':True,
            'charId':'chr_future','decoItemConfig':{'decoItemData':[
                {'prefabPath':'independent_prop','mountPoint':'source_slot'},
                {'prefabPath':'missing_prop','mountPoint':''}]}}}]}}


class DisplayPropTests(unittest.TestCase):
    def test_discovery_scans_nested_prefabs_without_a_roster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary)/'resources.sqlite'
            with closing(sqlite3.connect(database)) as connection:
                connection.execute('CREATE TABLE meta (key TEXT,value TEXT)')
                connection.execute("INSERT INTO meta VALUES ('format','EndfieldResourceDB/1')")
                connection.execute('CREATE TABLE paths (path TEXT,path_folded TEXT)')
                paths = (RESOURCE_PREFIX+'nested/new_prop.prefab', RESOURCE_PREFIX+'other.asset',
                         RESOURCE_PREFIX+'new_prop.prefab', 'assets/unrelated/new_prop.prefab')
                connection.executemany('INSERT INTO paths VALUES (?,?)',((path,path.casefold()) for path in paths))
                connection.commit()
            self.assertEqual(display_prop_resource_paths(database),
                (RESOURCE_PREFIX+'nested/new_prop.prefab',RESOURCE_PREFIX+'new_prop.prefab'))

    def test_relationship_comes_only_from_exact_managed_reference(self) -> None:
        path = RESOURCE_PREFIX+'independent_prop.prefab'
        paths = (path,RESOURCE_PREFIX+'chr_future_unrelated.prefab')
        names = {'chr_future':CharacterIdentity('chr_future','未来','Future','123')}
        bindings = display_prop_bindings(configuration(),paths,names)
        self.assertEqual(bindings[0].character_name,'未来')
        self.assertEqual(bindings[0].mount_point,'source_slot')
        self.assertEqual(bindings[1].status,'missing_resource')
        provider = CharacterResourceProvider('main/root.ab',())
        entries = assemble_display_prop_entries(paths,bindings,{path:(provider,)})
        unbound = next(row for row in entries if 'unrelated' in row.resource_path)
        self.assertEqual(unbound.bindings,())
        self.assertEqual(unbound.providers,())
        self.assertEqual(next(row for row in entries if row.resource_path == path).bindings,(bindings[0],))

    def test_ambiguous_basename_is_retained_without_assigning_ownership(self) -> None:
        paths = (RESOURCE_PREFIX+'independent_prop.prefab',RESOURCE_PREFIX+'nested/independent_prop.prefab')
        bindings = display_prop_bindings(configuration(),paths,{})
        self.assertEqual(bindings[0].status,'ambiguous_resource')
        self.assertEqual(bindings[0].resource_paths,paths)
        self.assertTrue(all(not entry.bindings for entry in assemble_display_prop_entries(paths,bindings,{})))

    def test_wrong_character_reference_or_partial_decode_fails(self) -> None:
        for field,value in (('charId','wrong'),('exactTypeTreeDecoded',False)):
            document = configuration()
            references = document['references']
            assert isinstance(references,dict)
            rows = references['RefIds']
            assert isinstance(rows,list)
            rows[0]['data'][field] = value
            with self.assertRaises(DisplayPropError):
                display_prop_bindings(document,(),{})

    def test_duplicate_reference_or_mismatched_dictionary_fails(self) -> None:
        document = configuration()
        document['data'] = {'_keys':['chr_future','chr_future'],'_values':[{'rid':17},{'rid':17}]}
        with self.assertRaises(DisplayPropError):
            display_prop_bindings(document,(),{})
        document['data'] = {'_keys':['chr_future'],'_values':[{'rid':19}]}
        with self.assertRaises(DisplayPropError):
            display_prop_bindings(document,(),{})

    def test_selection_requires_current_unique_id_and_provider(self) -> None:
        path = RESOURCE_PREFIX+'independent_prop.prefab'
        provider = CharacterResourceProvider('main/root.ab',())
        entry = assemble_display_prop_entries((path,),(),{path:(provider,)})[0]
        catalog = DisplayPropCatalog('EndfieldDisplayPropCatalog/1','catalog','characters','config',
            (entry,),(),(),'game','reader','2021.3.34f5')
        self.assertEqual(select_display_props(catalog,(entry.prop_id,)),(entry,))
        for selection in ((),('missing',),(entry.prop_id,entry.prop_id)):
            with self.assertRaises(DisplayPropError):
                select_display_props(catalog,selection)
        for providers in ((),(provider,provider)):
            with self.assertRaises(DisplayPropError):
                select_display_props(replace(catalog,entries=(replace(entry,providers=providers),)),(entry.prop_id,))
        public = public_display_prop_catalog(catalog)
        self.assertNotIn('logical_name',str(public))
        self.assertNotIn('source_stamps',public)

    def test_prepared_artifacts_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'source.bin').write_bytes(b'source')
            self.assertEqual(_artifact_path(root,'source.bin'),root/'source.bin')
            for path in ('../source.bin','C:/source.bin','/source.bin','folder//source.bin','./source.bin'):
                with self.assertRaises(DisplayPropError):
                    _artifact_path(root,path)


if __name__ == '__main__':
    unittest.main()
