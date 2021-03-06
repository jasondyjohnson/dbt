from dataclasses import dataclass, field
from typing import TypeVar, MutableMapping, Mapping, Union, List

from hologram import JsonSchemaMixin

from dbt.contracts.graph.manifest import SourceFile, RemoteFile, FileHash
from dbt.contracts.graph.parsed import (
    ParsedNode, HasUniqueID, ParsedMacro, ParsedDocumentation, ParsedNodePatch,
    ParsedSourceDefinition, ParsedAnalysisNode, ParsedHookNode, ParsedRPCNode,
    ParsedModelNode, ParsedSeedNode, ParsedTestNode, ParsedSnapshotNode,
)
from dbt.contracts.util import Writable, Replaceable
from dbt.exceptions import (
    raise_duplicate_resource_name, raise_duplicate_patch_name,
    CompilationException, InternalException
)
from dbt.version import __version__


# Parsers can return anything as long as it's a unique ID
ParsedValueType = TypeVar('ParsedValueType', bound=HasUniqueID)


def _check_duplicates(
    value: HasUniqueID, src: Mapping[str, HasUniqueID]
):
    if value.unique_id in src:
        raise_duplicate_resource_name(value, src[value.unique_id])


ManifestNodes = Union[
    ParsedAnalysisNode,
    ParsedHookNode,
    ParsedModelNode,
    ParsedSeedNode,
    ParsedTestNode,
    ParsedSnapshotNode,
    ParsedRPCNode,
]


def dict_field():
    return field(default_factory=dict)


@dataclass
class ParseResult(JsonSchemaMixin, Writable, Replaceable):
    vars_hash: FileHash
    profile_hash: FileHash
    project_hashes: MutableMapping[str, FileHash]
    nodes: MutableMapping[str, ManifestNodes] = dict_field()
    sources: MutableMapping[str, ParsedSourceDefinition] = dict_field()
    docs: MutableMapping[str, ParsedDocumentation] = dict_field()
    macros: MutableMapping[str, ParsedMacro] = dict_field()
    patches: MutableMapping[str, ParsedNodePatch] = dict_field()
    files: MutableMapping[str, SourceFile] = dict_field()
    disabled: MutableMapping[str, List[ParsedNode]] = dict_field()
    dbt_version: str = __version__

    def get_file(self, source_file: SourceFile) -> SourceFile:
        key = source_file.search_key
        if key is None:
            return source_file
        if key not in self.files:
            self.files[key] = source_file
        return self.files[key]

    def add_source(
        self, source_file: SourceFile, node: ParsedSourceDefinition
    ):
        # nodes can't be overwritten!
        _check_duplicates(node, self.sources)
        self.sources[node.unique_id] = node
        self.get_file(source_file).sources.append(node.unique_id)

    def add_node(self, source_file: SourceFile, node: ManifestNodes):
        # nodes can't be overwritten!
        _check_duplicates(node, self.nodes)
        self.nodes[node.unique_id] = node
        self.get_file(source_file).nodes.append(node.unique_id)

    def add_disabled(self, source_file: SourceFile, node: ParsedNode):
        if node.unique_id in self.disabled:
            self.disabled[node.unique_id].append(node)
        else:
            self.disabled[node.unique_id] = [node]
        self.get_file(source_file).nodes.append(node.unique_id)

    def add_macro(self, source_file: SourceFile, macro: ParsedMacro):
        # macros can be overwritten (should they be?)
        self.macros[macro.unique_id] = macro
        self.get_file(source_file).macros.append(macro.unique_id)

    def add_doc(self, source_file: SourceFile, doc: ParsedDocumentation):
        # Docs also can be overwritten (should they be?)
        self.docs[doc.unique_id] = doc
        self.get_file(source_file).docs.append(doc.unique_id)

    def add_patch(self, source_file: SourceFile, patch: ParsedNodePatch):
        # matches can't be overwritten
        if patch.name in self.patches:
            raise_duplicate_patch_name(patch.name, patch,
                                       self.patches[patch.name])
        self.patches[patch.name] = patch
        self.get_file(source_file).patches.append(patch.name)

    def _get_disabled(
        self, unique_id: str, match_file: SourceFile
    ) -> List[ParsedNode]:
        if unique_id not in self.disabled:
            raise InternalException(
                'called _get_disabled with id={}, but it does not exist'
                .format(unique_id)
            )
        return [
            n for n in self.disabled[unique_id]
            if n.original_file_path == match_file.path.original_file_path
        ]

    def sanitized_update(
        self, source_file: SourceFile, old_result: 'ParseResult',
    ) -> bool:
        """Perform a santized update. If the file can't be updated, invalidate
        it and return false.
        """
        if isinstance(source_file.path, RemoteFile):
            return False

        old_file = old_result.get_file(source_file)
        for doc_id in old_file.docs:
            doc = _expect_value(doc_id, old_result.docs, old_file, "docs")
            self.add_doc(source_file, doc)

        for macro_id in old_file.macros:
            macro = _expect_value(
                macro_id, old_result.macros, old_file, "macros"
            )
            self.add_macro(source_file, macro)

        for source_id in old_file.sources:
            source = _expect_value(
                source_id, old_result.sources, old_file, "sources"
            )
            self.add_source(source_file, source)

        # because we know this is how we _parsed_ the node, we can safely
        # assume if it's disabled it was done by the project or file, and
        # we can keep our old data
        for node_id in old_file.nodes:
            if node_id in old_result.nodes:
                node = old_result.nodes[node_id]
                self.add_node(source_file, node)
            elif node_id in old_result.disabled:
                matches = old_result._get_disabled(node_id, source_file)
                for match in matches:
                    self.add_disabled(source_file, match)
            else:
                raise CompilationException(
                    'Expected to find "{}" in cached "manifest.nodes" or '
                    '"manifest.disabled" based on cached file information: {}!'
                    .format(node_id, old_file)
                )

        for name in old_file.patches:
            patch = _expect_value(
                name, old_result.patches, old_file, "patches"
            )
            self.add_patch(source_file, patch)

        return True

    def has_file(self, source_file: SourceFile) -> bool:
        key = source_file.search_key
        if key is None:
            return False
        if key not in self.files:
            return False
        my_checksum = self.files[key].checksum
        return my_checksum == source_file.checksum

    @classmethod
    def rpc(cls):
        # ugh!
        return cls(FileHash.empty(), FileHash.empty(), {})


T = TypeVar('T')


def _expect_value(
    key: str, src: Mapping[str, T], old_file: SourceFile, name: str
) -> T:
    if key not in src:
        raise CompilationException(
            'Expected to find "{}" in cached "result.{}" based '
            'on cached file information: {}!'
            .format(key, name, old_file)
        )
    return src[key]
