from glob import glob
import faiss
from colbert.utils.utils import print_message
import os
import torch
import pandas as pd
import pyterrier as pt
from pyterrier import tqdm
from pyterrier.transformer import TransformerBase
from pyterrier.datasets import Dataset
from typing import Union, Tuple
import random
from colbert.evaluation.load_model import load_model
from . import load_checkpoint
# monkeypatch to use our downloading version
import colbert.evaluation.loaders
colbert.evaluation.loaders.load_checkpoint = load_checkpoint
colbert.evaluation.loaders.load_model.__globals__['load_checkpoint'] = load_checkpoint
from colbert.modeling.inference import ModelInference
from colbert.evaluation.slow import slow_rerank
from colbert.indexing.loaders import get_parts, load_doclens
import colbert.modeling.colbert
from collections import defaultdict
import numpy as np
import pickle
from warnings import warn

class file_part_mmap:
    def __init__(self, file_path, file_doclens):
        self.dim = 128 # TODO
        
        self.doclens = file_doclens
        self.endpos = np.cumsum(self.doclens)
        self.startpos = self.endpos - self.doclens

        mmap_storage = torch.HalfStorage.from_file(file_path, False, sum(self.doclens) * self.dim)
        self.mmap = torch.HalfTensor(mmap_storage).view(sum(self.doclens), self.dim)
 
    def get_embedding(self, pid):
        startpos = self.startpos[pid]
        endpos = self.endpos[pid]
        return self.mmap[startpos:endpos,:]

class file_part_mem:
    def __init__(self, file_path, file_doclens):
        self.dim = 128 # TODO
        
        self.doclens = file_doclens
        self.endpos = np.cumsum(self.doclens)
        self.startpos = self.endpos - self.doclens

        self.mmap = torch.load(file_path)
        #print(self.mmap.shape)
 
    def get_embedding(self, pid):
        startpos = self.startpos[pid]
        endpos = self.endpos[pid]
        return self.mmap[startpos:endpos,:]


class Object(object):
    pass


from typing import List     


class re_ranker_mmap:
    def __init__(self, index_path, args, inference, verbose = False, memtype='mmap'):
        self.args = args
        self.doc_maxlen = args.doc_maxlen
        assert self.doc_maxlen > 0
        self.inference = inference
        self.dim = 128 #TODO
        self.verbose = verbose
    
        # Every pt file gets its own list of doc lengths
        self.part_doclens = load_doclens(index_path, flatten=False)
        assert len(self.part_doclens) > 0, "Did not find any indices at %s" % index_path
        # Local mmapped tensors with local, single file accesses
        self.part_mmap : List[file_part_mmap] = re_ranker_mmap._load_parts(index_path, self.part_doclens, memtype)
        
        # last pid (inclusive, e.g., the -1) in each pt file
        # the -1 is used in the np.searchsorted
        # so if each partition has 1000 docs, the array is [999, 1999, ...]
        # this helps us map from passage id to part (inclusive, explaning the -1)
        self.part_pid_end_offsets = np.cumsum([len(x) for x in self.part_doclens]) - 1
        
        # first pid (inclusive) in each pt file
        tmp = np.cumsum([len(x) for x in self.part_doclens])
        tmp[-1] = 0
        self.part_pid_begin_offsets = np.roll(tmp, 1)
        # [0, 1000, 2000, ...]
        self.part_pid_begin_offsets
    
    @staticmethod
    def _load_parts(index_path, part_doclens, memtype="mmap"):
        # Every pt file is loaded and managed independently, with local pids
        _, all_parts_paths, _ = get_parts(index_path)
        
        if memtype == "mmap":
            all_parts_paths = [ file.replace(".pt", ".store") for file in all_parts_paths ]
            mmaps = [file_part_mmap(path, doclens) for path, doclens in zip(all_parts_paths, part_doclens)]
        elif memtype == "mem":
            mmaps = [file_part_mem(path, doclens) for path, doclens in tqdm(zip(all_parts_paths, part_doclens), total=len(all_parts_paths), desc="Loading index shards to memory", unit="shard")]
        else:
            assert False, "Unknown memtype %s" % memtype
        return mmaps

    def get_embedding(self, pid):
        # In which pt file we need to look the given pid
        part_id = np.searchsorted(self.part_pid_end_offsets, pid)
        # calculate the pid local to the correct pt file
        local_pid = pid - self.part_pid_begin_offsets[part_id]
        # identify the tensor we look for
        disk_tensor = self.part_mmap[part_id].get_embedding(local_pid)
        doclen = disk_tensor.shape[0]
         # only here is there a memory copy from the memory mapped file 
        target = torch.zeros(self.doc_maxlen, self.dim)
        target[:doclen, :] = disk_tensor
        return target
    
    def get_embedding_copy(self, pid, target, index):
        # In which pt file we need to look the given pid
        part_id = np.searchsorted(self.part_pid_end_offsets, pid)
        # calculate the pid local to the correct pt file
        local_pid = pid - self.part_pid_begin_offsets[part_id]
        # identify the tensor we look for
        disk_tensor = self.part_mmap[part_id].get_embedding(local_pid)
        doclen = disk_tensor.shape[0]
        # only here is there a memory copy from the memory mapped file 
        target[index, :doclen, :] = disk_tensor
        return target
    
    def our_rerank(self, query, pids, gpu=True):
        colbert = self.args.colbert
        inference = self.inference

        Q = inference.queryFromText([query])
        if self.verbose:
            pid_iter = tqdm(pids, desc="lookups", unit="d")
        else:
            pid_iter = pids

        D_ = torch.zeros(len(pids), self.doc_maxlen, self.dim)
        for offset, pid in enumerate(pid_iter):
            self.get_embedding_copy(pid, D_, offset)

        if gpu:
            D_ = D_.cuda()

        scores = colbert.score(Q, D_).cpu()
        del(D_)
        return scores.tolist()

    def our_rerank_batched(self, query, pids, gpu=True, batch_size=1000):
        import more_itertools
        if len(pids) < batch_size:
            return self.our_rerank(query, pids, gpu=gpu)
        allscores=[]
        for group in more_itertools.chunked(pids, batch_size):
            batch_scores = self.our_rerank(query, group, gpu)
            allscores.extend(batch_scores)
        return allscores
        
        
    def our_rerank_with_embeddings(self, qembs, pids, weightsQ=None, gpu=True):
        """
        input: qid,query, docid, query_tokens, query_embeddings, query_weights 
        
        output: qid, query, docid, score
        """
        colbert = self.args.colbert
        inference = self.inference
        # default is uniform weight for all query embeddings
        if weightsQ is None:
            weightsQ = torch.ones(len(qembs))
        # make to 3d tensor
        Q = torch.unsqueeze(qembs, 0)
        if gpu:
            Q = Q.cuda()
        
        if self.verbose:
            pid_iter = tqdm(pids, desc="lookups", unit="d")
        else:
            pid_iter = pids

        D_ = torch.zeros(len(pids), self.doc_maxlen, self.dim)
        for offset, pid in enumerate(pid_iter):
            self.get_embedding_copy(pid, D_, offset)
        if gpu:
            D_ = D_.cuda()
        maxscoreQ = (Q @ D_.permute(0, 2, 1)).max(2).values.cpu()
        scores = (weightsQ*maxscoreQ).sum(1).cpu()
        return scores.tolist()

    def our_rerank_with_embeddings_batched(self, qembs, pids, weightsQ=None, gpu=True, batch_size=1000):
        import more_itertools
        if len(pids) < batch_size:
            return self.our_rerank_with_embeddings(qembs, pids, weightsQ, gpu)
        allscores=[]
        for group in more_itertools.chunked(pids, batch_size):
            batch_scores = self.our_rerank_with_embeddings(qembs, group, weightsQ, gpu)
            allscores.extend(batch_scores)
        return allscores


class np_re_ranker_mmap:
    def __init__(self, index_path, args, inference, verbose = False):
        self.args = args
        self.doc_maxlen = args.doc_maxlen
        assert self.doc_maxlen > 0
        self.inference = inference
        self.dim = 128 #TODO
        self.verbose = verbose
    
        # Every pt file gets its own list of doc lengths
        self.doc_offsets = np.memmap(os.path.join(index_path, 'doclens.psum.np'), dtype=np.uint64, mode='r')
        vecs_path = os.path.join(index_path, 'vecs.np')
        vec_count = os.path.getsize(vecs_path) // 2 // self.dim # 2 for float16
        self.vecs = np.memmap(vecs_path, dtype=np.float16, mode='r', shape=(vec_count, self.dim))
        self.tok2idxs = np.memmap(os.path.join(index_path, 'tok2idxs.data.np'), mode='r', dtype=np.uint64)
        self.tok2idxs_offsets = np.memmap(os.path.join(index_path, 'tok2idxs.psum.np'), mode='r', dtype=np.uint64)

    def get_embedding(self, pid):
        return self.get_embedding_copy(pid, torch.zeros(1, self.doc_maxlen, self.dim), 0)[0]
    
    def get_embedding_copy(self, pid, target, index):
        start, stop = self.doc_offsets[pid:pid+2]
        disk_tensor = self.vecs[start:stop]
        doclen = disk_tensor.shape[0]
         # only here is there a memory copy from the memory mapped file 
        target[index, :doclen, :] = torch.from_numpy(disk_tensor)
        return target
    
    def our_rerank(self, query, pids, gpu=True):
        colbert = self.args.colbert
        inference = self.inference

        Q = inference.queryFromText([query])
        if self.verbose:
            pid_iter = tqdm(pids, desc="lookups", unit="d")
        else:
            pid_iter = pids

        # TODO: I bet scoring could be much faster if we avoid moving from numpy to torch.
        # We'd be able to:
        #   - re-use allocated memory for each document, rather than allocating enough space for
        #     each document (or potentially always rely directly on the mmap)
        # On the other hand:
        #   - we'd be giving up GPU acceleration
        #   - we'd need to re-create the scoring function in numpy (though it's not complicated)
        # For now, just keeping it as it was in the original implementation
        D_ = torch.zeros(len(pids), self.doc_maxlen, self.dim)
        for offset, pid in enumerate(pid_iter):
            self.get_embedding_copy(pid, D_, offset)

        if gpu:
            D_ = D_.cuda()

        scores = colbert.score(Q, D_).cpu()
        del(D_)
        return scores.tolist()

    def our_rerank_batched(self, query, pids, gpu=True, batch_size=1000):
        import more_itertools
        if len(pids) < batch_size:
            return self.our_rerank(query, pids, gpu=gpu)
        allscores=[]
        for group in more_itertools.chunked(pids, batch_size):
            batch_scores = self.our_rerank(query, group, gpu)
            allscores.extend(batch_scores)
        return allscores

    def our_rerank_with_embeddings(self, qembs, pids, weightsQ=None, gpu=True):
        """
        input: qid,query, docid, query_tokens, query_embeddings, query_weights 
        
        output: qid, query, docid, score
        """
        colbert = self.args.colbert
        inference = self.inference
        # default is uniform weight for all query embeddings
        if weightsQ is None:
            weightsQ = torch.ones(len(qembs))
        # make to 3d tensor
        Q = torch.unsqueeze(qembs, 0)
        if gpu:
            Q = Q.cuda()
        
        if self.verbose:
            pid_iter = tqdm(pids, desc="lookups", unit="d")
        else:
            pid_iter = pids

        D_ = torch.zeros(len(pids), self.doc_maxlen, self.dim)
        for offset, pid in enumerate(pid_iter):
            self.get_embedding_copy(pid, D_, offset)
        if gpu:
            D_ = D_.cuda()
        maxscoreQ = (Q @ D_.permute(0, 2, 1)).max(2).values.cpu()
        scores = (weightsQ*maxscoreQ).sum(1).cpu()
        return scores.tolist()

    def our_rerank_with_embeddings_batched(self, qembs, pids, weightsQ=None, gpu=True, batch_size=1000):
        import more_itertools
        if len(pids) < batch_size:
            return self.our_rerank_with_embeddings(qembs, pids, weightsQ, gpu)
        allscores=[]
        for group in more_itertools.chunked(pids, batch_size):
            batch_scores = self.our_rerank_with_embeddings(qembs, group, weightsQ, gpu)
            allscores.extend(batch_scores)
        return allscores

    def vecs_by_idxs(self, idxs, max_count=None):
        if max_count and len(idxs) > max_count:
            if self.verbose:
                print(f"sampling max_count={max_count} from {len(idxs)} vectors")
            rng = np.random.RandomState(42)
            idxs = rng.choice(idxs, size=max_count, replace=False)
            idxs.sort() # faster lookups if in sequence
        if self.verbose:
            return np.concatenate([self.vecs[idxs[start:start+100]] for start in pt.tqdm(range(0, len(idxs), 100), desc='looking up vectors', unit='chunk')])
        return self.vecs[idxs]

    def vecs_by_tok(self, tok, max_count=None):
        start, stop = self.tok2idxs_offsets[tok:tok+2]
        idxs = self.tok2idxs[start:stop]
        return self.vecs_by_idxs(idxs, max_count=max_count)

    def catvecs_by_tok_seq(self, toks, max_count=None, return_idxs=False, ensure_ranges=None):
        tok_idxs = None
        for i, tok in enumerate(toks):
            start, stop = self.tok2idxs_offsets[tok:tok+2]
            idxs = self.tok2idxs[start:stop]
            if i == 0:
                tok_idxs = idxs
            else:
                seq_mask = np.isin(tok_idxs, idxs - i, assume_unique=True)
                tok_idxs = tok_idxs[seq_mask]
        if max_count and len(tok_idxs) > max_count:
            rng = np.random.RandomState(42)
            rand_idxs = rng.choice(tok_idxs, size=max_count, replace=False)
            if ensure_ranges:
                conditions = ((tok_idxs >= ensure_ranges[0][0]) & (tok_idxs < ensure_ranges[0][1]))
                for start, stop in ensure_ranges[1:]:
                    conditions = conditions | ((tok_idxs >= start) & (tok_idxs < stop))
                tok_idxs = np.concatenate([rand_idxs, tok_idxs[conditions]])
            else:
                tok_idxs = rand_idxs
            tok_idxs = np.sort(tok_idxs) # faster lookups if in sequence
        vecs = []
        orig_tok_idxs = tok_idxs
        for _ in toks:
            vecs.append(self.vecs_by_idxs(tok_idxs))
            tok_idxs = tok_idxs + 1
        vecs = np.concatenate(vecs, axis=1)
        if return_idxs:
            return vecs, tok_idxs
        return vecs

    def vecs_by_text(self, text, max_count=None, return_idxs=False, ensure_ranges=None):
        toks = self.inference.query_tokenizer.encode([text])[0]
        return self.catvecs_by_tok_seq(toks, max_count, return_idxs=return_idxs, ensure_ranges=ensure_ranges)


class ColBERTFactory():

    def __init__(self, 
            colbert_model : Union[str, Tuple[colbert.modeling.colbert.ColBERT, dict]], 
            index_root : str, 
            index_name : str,
            faiss_partitions=None,#TODO 100-
            memtype = "mem",
            gpu=True):
        
        args = Object()
        args.query_maxlen = 32
        args.doc_maxlen = 180
        args.dim = 128
        args.bsize = 128
        args.similarity = 'cosine'        
        args.dim = 128
        args.amp = True
        args.nprobe = 10
        args.part_range = None
        args.mask_punctuation = False
        args.partitions = faiss_partitions

        self._init_args = [
            colbert_model,
            index_root,
            index_name,
        ]
        self._init_kwargs ={
            'faiss_partitions' : faiss_partitions,
            'memtype' : memtype,
            'gpu' : gpu
        }

        self.verbose = False
        self._faissnn = None
        self.index_root = index_root
        self.index_name = index_name
        if index_root is None or index_name is None:
            warn("No index_root and index_name specified - no index ranking possible")
        else:
            self.index_path = os.path.join(index_root, index_name)
            if os.path.exists(os.path.join(self.index_path, "docnos.np")):
                with open(os.path.join(self.index_path, "docnos.np.meta"), 'rt') as f:
                    meta = f.read().strip().split()
                    if len(meta) == 2:
                        l, self._docno_prefix = int(meta[0]), meta[1]
                    else:
                        l, self._docno_prefix = int(meta[0]), ''
                self._docno_mmap = np.memmap(os.path.join(self.index_path, "docnos.np"), mode='r', dtype=f'S{l}')
                self._docno_sorted_mmap = np.memmap(os.path.join(self.index_path, "docnos.np.sorted"), mode='r', dtype=f'S{l}')
                self._docno_sorted_idxs_mmap = np.memmap(os.path.join(self.index_path, "docnos.np.sorted.idxs"), mode='r', dtype=np.uint64)
                self.docid_as_docno = False
            else:
                docnos_file = os.path.join(self.index_path, "docnos.pkl.gz")
                if os.path.exists(docnos_file):
                    with pt.io.autoopen(docnos_file, "rb") as f:
                        self._docid2docno = pickle.load(f)
                        # support reverse docno lookup in memory
                        self._docno2docid = { docno : docid for docid, docno in enumerate(self._docid2docno) }
                        self.docid_as_docno = False
                else:
                    self.docid_as_docno = True

        try:
            import faiss
        except: # TODO: shouldn't this be ImportError?
            warn("Faiss not installed. You cannot do retrieval")

        self.faiss_index_on_gpu = True
        self.gpu = True
        if not gpu:
            self.faiss_index_on_gpu = False
            warn("Gpu disabled, YMMV")
            import colbert.parameters
            import colbert.evaluation.load_model
            import colbert.modeling.colbert
            colbert.parameters.DEVICE = colbert.evaluation.load_model.DEVICE = colbert.modeling.colbert.DEVICE = torch.device("cpu")
            self.gpu = False

        if isinstance (colbert_model, str):
            args.checkpoint = colbert_model
            args.colbert, args.checkpoint = load_model(args)
        else:
            assert isinstance(colbert_model, tuple)
            args.colbert, args.checkpoint = colbert_model
            from colbert.modeling.colbert import ColBERT
            assert isinstance(args.colbert, ColBERT)
            assert isinstance(args.checkpoint, dict)
            
        args.inference = ModelInference(args.colbert, amp=args.amp)
        self.args = args

        self.memtype = memtype

        #we load this lazily
        self.rrm = None
        self.faiss_index = None
        
    def __reduce_ex__(self, proto):
        return (
            ColBERTFactory,
            (*self._init_args, *self._init_kwargs),
            None
        )

    def __get_state__(self):
        return None
    
    
    #allows a colbert index to be built from a dataset
    def from_dataset(dataset : Union[str,Dataset], 
            variant : str = None, 
            version='latest',            
            **kwargs):
        
        from pyterrier.batchretrieve import _from_dataset
        
        #colbertfactory doesnt match quite the expectations, so we can use a wrapper fb
        def _construct(folder, **kwargs):
            import os
            index_loc = os.path.dirname(folder)
            index_name = os.path.dirname(folder)
            checkpoint = kwargs.get('colbert_factory')
            del(kwargs['colbert_factory'])
            return ColBERTFactory(checkpoint, index_loc, index_name, **kwargs)
        
        return _from_dataset(dataset, 
                             variant=variant, 
                             version=version, 
                             clz=_construct)
        
        
    def _rrm(self):
        """
        Returns an instance of the re_ranker_mmap class.
        Only one is created, if necessary.
        """

        if self.rrm is not None:
            return self.rrm
        print("Loading reranking index, memtype=%s" % self.memtype)
        if os.path.exists(os.path.join(self.index_path, 'vecs.np')):
            self.rrm = np_re_ranker_mmap(
                self.index_path, 
                self.args, 
                self.args.inference, 
                verbose=self.verbose)
        else:
            self.rrm = re_ranker_mmap(
                self.index_path, 
                self.args, 
                self.args.inference, 
                verbose=self.verbose, 
                memtype=self.memtype)
        return self.rrm
        
    def nn_term(self, df=False):
        """
        Returns an instance of the FaissNNTerm class, which provides statistics about terms
        """
        if self._faissnn is not None:
            return self._faissnn
        from .faiss_term_index import FaissNNTerm
        #TODO accept self.args.inference as well
        self._faissnn = FaissNNTerm(
            self.args.colbert,
            self.index_root,
            self.index_name,
            faiss_index = self._faiss_index(),
            df=df, verbose=self.verbose)
        return self._faissnn

    def query_encoder(self, detach=True) -> TransformerBase:
        """
        Returns a transformer that can encode queries using ColBERT's model.
        input: qid, query
        output: qid, query, query_embs, query_toks,
        """
        def _encode_query(row):
            with torch.no_grad():
                Q, ids, masks = self.args.inference.queryFromText([row.query], bsize=512, with_ids=True)
                if detach:
                    Q = Q.cpu()
                return pd.Series([Q[0], ids[0]])
            
        def row_apply(df):
            df[["query_embs", "query_toks"]] = df.apply(_encode_query, axis=1)
            return df
        
        return pt.apply.generic(row_apply)

    def _faiss_index(self):
        """
        Returns an instance of the Colbert FaissIndex class, which provides nearest neighbour information
        """
        from colbert.indexing.faiss import get_faiss_index_name
        from colbert.ranking.faiss_index import FaissIndex
        if self.faiss_index is not None:
            return self.faiss_index
        if os.path.exists(f'{self.index_path}/0.faiss'):
            self.faiss_index = MultiFaissMmapIndex(self.index_path, self.args.nprobe)
        else:
            faiss_index_path = get_faiss_index_name(self.args)
            faiss_index_path = os.path.join(self.index_path, faiss_index_path)
            if not os.path.exists(faiss_index_path):
                raise ValueError("No faiss index found at %s" % faiss_index_path)
            self.faiss_index = FaissIndex(self.index_path, faiss_index_path, self.args.nprobe, self.args.part_range)
            # ensure the faiss_index is transferred to GPU memory for speed
            import faiss
            if self.faiss_index_on_gpu:
                self.faiss_index.faiss_index = faiss.index_cpu_to_all_gpus(self.faiss_index.faiss_index)
        return self.faiss_index

    def set_retrieve(self, batch=False, query_encoded=False, faiss_depth=1000, verbose=False, docnos=False) -> TransformerBase:
        #input: qid, query
        #OR
        #input: qid, query, query_embs, query_toks, query_weights

        #output: qid, query, docid, [docno]
        #OR
        #output: qid, query, query_embs, query_toks, query_weights, docid, [docno]
        
        assert not batch
        faiss_index = self._faiss_index()
        
        # this is when queries have NOT already been encoded
        def _single_retrieve(queries_df):
            rtr = []
            iter = queries_df.itertuples()
            iter = tqdm(iter, unit="q")  if verbose else iter
            for row in iter:
                qid = row.qid
                query = row.query
                with torch.no_grad():
                    Q, ids, masks = self.args.inference.queryFromText([query], bsize=512, with_ids=True)
                Q_f = Q[0:1, :, :]
                all_pids = faiss_index.retrieve(faiss_depth, Q_f, verbose=verbose)
                Q_cpu = Q[0, :, :].cpu()
                for passage_ids in all_pids:
                    if verbose:
                        print("qid %s retrieved docs %d" % (qid, len(passage_ids)))
                    for pid in passage_ids:
                        rtr.append([qid, query, pid, ids[0], Q_cpu])
                        
            #build the DF to return for this query
            rtrDf = pd.DataFrame(rtr, columns=["qid","query",'docid','query_toks','query_embs'] )
            if docnos:
                rtrDf = self._add_docnos(rtrDf)
            return rtrDf

        # this is when queries have already been encoded
        def _single_retrieve_qembs(queries_df):
            rtr = []
            query_weights = "query_weights" in queries_df.columns
            iter = queries_df.itertuples()
            iter = tqdm(iter, unit="q") if verbose else iter
            for row in iter:
                qid = row.qid
                embs = row.query_embs
                Q_f = torch.unsqueeze(embs, 0)
                all_pids = faiss_index.retrieve(faiss_depth, Q_f, verbose=verbose)
                for passage_ids in all_pids:
                    if verbose:
                        print("qid %s retrieved docs %d" % (qid, len(passage_ids)))
                    for pid in passage_ids:
                        if query_weights:
                           rtr.append([qid, row.query, pid, row.query_toks, row.query_embs, row.query_weights])
                        else:
                           rtr.append([qid, row.query, pid, row.query_toks, row.query_embs])
            
            #build the DF to return for this query
            cols = ["qid","query",'docid','query_toks','query_embs']
            if query_weights:
                cols.append("query_weights")
            rtrDf = pd.DataFrame(rtr, columns=cols)
            if docnos:
                rtrDf = self._add_docnos(rtrDf)
            return rtrDf
        
        return pt.apply.generic(_single_retrieve_qembs if query_encoded else _single_retrieve)

    def set_retrieve_approx(self, batch=False, query_encoded=False, faiss_depth=1000, verbose=False, maxsim=False) -> TransformerBase:
        def _single_retrieve(queries_df):
            rtr = []
            iter = queries_df.itertuples()
            iter = tqdm(iter, unit="q") if verbose else iter
            for row in iter:
                qid = row.qid
                if query_encoded:
                    embs = row.query_embs
                    qtoks = row.query_toks
                    ids = np.expand_dims(qtoks, axis=0)
                    Q_cpu = embs.cpu()
                    Q_cpu_numpy = embs.float().numpy()
                else:
                    with torch.no_grad():
                        Q, ids, masks = self.args.inference.queryFromText([row.query], bsize=512, with_ids=True)
                    Q_f = Q[0:1, :, : ]
                    Q_cpu = Q[0, :, :].cpu()
                    Q_cpu_numpy = Q_cpu.float().numpy()
                
                if hasattr(self._faiss_index(), 'faiss_index'):
                    all_scores, all_embedding_ids = self._faiss_index().faiss_index.search(Q_cpu_numpy, faiss_depth)
                else:
                    all_scores, all_embedding_ids = self._faiss_index().search(Q_cpu_numpy, faiss_depth, verbose=verbose)
                pid2score = defaultdict(float)
                for qpos in range(len(ids)):
                    scores = all_scores[qpos]
                    embedding_ids = all_embedding_ids[qpos]
                    if hasattr(self.faiss_index, 'emb2pid'):
                        pids = self.faiss_index.emb2pid[embedding_ids]
                    else:
                        pids = np.searchsorted(self.faiss_index.doc_offsets, embedding_ids, side='right') - 1
                    if maxsim:
                        qpos_scores = defaultdict(float)
                        for (score, pid) in zip(scores, pids):
                            _pid = int(pid)
                            qpos_scores[_pid] = max(qpos_scores[_pid], score)
                        for (pid, score) in qpos_scores.items():
                            pid2score[pid] += score
                    else:
                        for (score, pid) in zip(scores, pids):
                            pid2score[int(pid)] += score
                for pid, score in pid2score.items():
                    rtr.append([qid, row.query, pid, score, ids[0], Q_cpu])

            #TODO this _add_docnos shouldnt be needed
            return self._add_docnos( pt.model.add_ranks(pd.DataFrame(rtr, columns=["qid","query",'docid', 'score','query_toks','query_embs'])) )
        t = pt.apply.by_query(_single_retrieve, add_ranks=False, verbose=verbose)
        import types
        def __reduce_ex__(t2, proto):
            kwargs = { 'batch':batch, 'query_encoded': query_encoded, 'faiss_depth' : faiss_depth, 'maxsim': maxsim}
            return (
                set_retrieve_approx,
                #self is the factory, and it will be serialised using its own __reduce_ex__ method
                (self, [], kwargs),
                None
            )
        t.__reduce_ex__ = types.MethodType(__reduce_ex__, t)
        t.__getstate__ = types.MethodType(lambda t2 : None, t)
        return t


    def text_scorer(self, query_encoded=False, doc_attr="text", verbose=False) -> TransformerBase:
        """
        Returns a transformer that uses ColBERT model to score the *text* of documents.
        """
        #input: qid, query, docno, text
        #OR
        #input: qid, query, query_embs, query_toks, query_weights, docno, text

        #output: qid, query, docno, score

        assert not query_encoded
        def _text_scorer(queries_and_docs):
            groupby = queries_and_docs.groupby("qid")
            rtr=[]
            with torch.no_grad():
                for qid, group in tqdm(groupby, total=len(groupby), unit="q") if verbose else groupby:
                    query = group["query"].values[0]
                    ranking = slow_rerank(self.args, query, group["docno"].values, group[doc_attr].values.tolist())
                    for rank, (score, pid, passage) in enumerate(ranking):
                            rtr.append([qid, query, pid, score, rank])          
            return pd.DataFrame(rtr, columns=["qid", "query", "docno", "score", "rank"])

        return pt.apply.generic(_text_scorer)

    def _add_docids(self, df):
        if self.docid_as_docno:
            df["docid"] = df["docno"].astype('int64')
        else:
            df["docid"] = df["docno"].apply(lambda docno : self.docno2docid(docno))
        return df

    def _add_docnos(self, df):
        if self.docid_as_docno:
            df["docno"] = df["docid"].astype('str')
        else:
            df["docno"] = df["docid"].apply(lambda docid : self.docid2docno(docid))
        return df

    def docid2docno(self, docid):
        if self._docno_mmap is not None:
            res = self._docno_mmap[docid]
            return self._docno_prefix + res.decode()
        else:
            return self._docid2docno[docid]

    def docno2docid(self, docno):
        if self._docno_sorted_mmap is not None:
            assert docno.startswith(self._docno_prefix)
            docno_bytes = docno[len(self._docno_prefix):].encode()
            sorted_idx = np.searchsorted(self._docno_sorted_mmap, docno_bytes, side='left')
            if sorted_idx >= self._docno_sorted_mmap.shape[0] or self._docno_sorted_mmap[sorted_idx] != docno_bytes:
                print(f'cannot find docno {docno}')
                return None
            return self._docno_sorted_idxs_mmap[sorted_idx]
        else:
            return self._docno2docid[docno]

    def index_scorer(self, query_encoded=False, add_ranks=False, add_docnos=True, batch_size=10000, verbose=False) -> TransformerBase:
        """
        Returns a transformer that uses the ColBERT index to perform scoring of documents to queries 
        """
        #input: qid, query, [docno], [docid] 
        #OR
        #input: qid, query, query_embs, query_toks, query_weights, docno], [docid] 

        #output: qid, query, docno, score

        rrm = self._rrm()

        def rrm_scorer(qid_group):
            qid_group = qid_group.copy()
            if "docid" not in qid_group.columns:
                qid_group = self._add_docids(qid_group)
            qid_group.sort_values("docid", inplace=True)
            docids = qid_group["docid"].values
            if batch_size > 0:
                scores = rrm.our_rerank_batched(qid_group.iloc[0]["query"], docids, batch_size=batch_size)
            else:
                scores = rrm.our_rerank(qid_group.iloc[0]["query"], docids)
            qid_group["score"] = scores
            if "docno" not in qid_group.columns and add_docnos:
                qid_group = self._add_docnos(qid_group)
            if add_ranks:
                return pt.model.add_ranks(qid_group)
            return qid_group

        def rrm_scorer_query_embs(qid_group):
            qid_group = qid_group.copy()
            if "docid" not in qid_group.columns:
                qid_group = self._add_docids(qid_group)
            qid_group.sort_values("docid", inplace=True)
            docids = qid_group["docid"].values
            weights = None
            if "query_weights" in qid_group.columns:
                weights = qid_group.iloc[0].query_weights
            if batch_size > 0:
                scores = rrm.our_rerank_with_embeddings_batched(qid_group.iloc[0]["query_embs"], docids, weights, batch_size=batch_size, gpu=self.gpu)
            else:
                scores = rrm.our_rerank_with_embeddings(qid_group.iloc[0]["query_embs"], docids, weights, gpu=self.gpu)
            qid_group["score"] = scores
            if "docno" not in qid_group.columns and add_docnos:
                qid_group = self._add_docnos(qid_group)
            if add_ranks:
                return pt.model.add_ranks(qid_group)
            return qid_group

        if query_encoded:
            return pt.apply.by_query(rrm_scorer_query_embs, verbose=verbose, add_ranks=False)
        return pt.apply.by_query(rrm_scorer, verbose=verbose, add_ranks=False)

    def end_to_end(self) -> TransformerBase:
        """
        Returns a transformer composition that uses a ColBERT FAISS index to retrieve documents, followed by a ColBERT index 
        to perform accurate scoring of the retrieved documents. Equivalent to `colbertfactory.set_retrieve() >> colbertfactory.index_scorer()`.
        """
        #input: qid, query, 
        #output: qid, query, docno, score
        return self.set_retrieve() >> self.index_scorer(query_encoded=True)

    def fetch_index_encodings(factory, verbose=False) -> TransformerBase:
        """
        New encoder that gets embeddings from rrm and stores into doc_emsb column
        input: docid, ...
        output: ditto + doc_embs
        """
        def _get_embs(self, df):
            if verbose:
                import pyterrier as pt
                pt.tqdm.pandas()
                df["doc_embs"] = df.docid.progress_apply(factory.rrm.get_embedding)
            else:
                df["doc_embs"] = df.docid.apply(factory.rrm.get_embedding)
            return df
        return pt.apply.by_query(_get_embs)

    def scorer(factory, verbose=False) -> TransformerBase:
        """
        Calculates the ColBERT max_sim operator using previous encodings of queries and documents
        input: qid, query_embs, [query_weights], docno, doc_embs
        output: ditto + score
        """
        import torch
        colbert = factory.args.colbert
        def _score_query(df):
            weightsQ = None
            Q = torch.cat([df.query_embs[0]])
            if "query_weights" in df.columns:
                weightsQ = df.iloc[0].query_weights
            else:
                weightsQ = torch.ones(Q.shape[0])        
            D = torch.zeros(len(df), factory.args.doc_maxlen, factory.args.dim)
            iter = range(len(df))
            if verbose:
                iter = pt.tqdm(iter, total=len(df))
            for i in iter:
                doc_embs = df.iloc[i].doc_embs
                doclen = doc_embs.shape[0]
                D[i, 0:doclen, :] =  doc_embs   
            maxscoreQ = (Q @ D.permute(0, 2, 1)).max(2).values.cpu()
            scores = (weightsQ*maxscoreQ).sum(1).cpu()
            df["score"] = scores.tolist()
            return df
            
        return pt.apply.by_query(_score_query)

    def doc_encoder(self, detach=True, text_attr='text', verbose=False) -> TransformerBase:
        """
        Returns a transformer that can encode passages using ColBERT's model.
        input: *, text
        output: *, text, doc_embs
        """
        def _encode_doc(row):
            import torch
            with torch.no_grad():
                Q = self.args.inference.docFromText([row[text_attr]], bsize=512)
                if detach:
                    Q = Q.cpu()
                return Q[0]

        def row_apply(df):
            import pyterrier as pt
            if verbose:
                pt.tqdm.pandas()
                df["doc_embs"] = df.progress_apply(_encode_doc, axis=1)
            else:
                df["doc_embs"] = df.apply(_encode_doc, axis=1)
            return df

        return pt.apply.generic(row_apply)

    def prf(pytcolbert, rerank, fb_docs=3, fb_embs=10, beta=1.0, k=24) -> TransformerBase:
        """
        Returns a pipeline for ColBERT PRF, either as a ranker, or a re-ranker. Final ranking is cutoff at 1000 docs.
    
        Parameters:
         - rerank(bool): Whether to rerank the initial documents, or to perform a new set retrieve to gather new documents.
         - fb_docs(int): Number of passages to use as feedback. Defaults to 3. 
         - k(int): Number of clusters to apply on the embeddings of the top K documents. Defaults to 24.
         - fb_embs(int): Number of expansion embeddings to add to the query. Defaults to 10.
         - beta(float): Weight of the new embeddings compared to the original emebddings. Defaults to 1.0.

        Reference:
        
        X. Wang, C. Macdonald, N. Tonellotto, I. Ounis. Pseudo-Relevance Feedback for Multiple Representation Dense Retrieval. 
        In Proceedings of ICTIR 2021.
        
        """
        #input: qid, query, 
        #output: qid, query, query_embs, query_toks, query_weights, docno, rank, score
        dense_e2e = pytcolbert.set_retrieve() >> pytcolbert.index_scorer(query_encoded=True, add_ranks=True, batch_size=10000)
        if rerank:
            prf_pipe = (
                dense_e2e  
                >> ColbertPRF(pytcolbert, k=k, fb_docs=fb_docs, fb_embs=fb_embs, beta=beta, return_docs=True)
                >> (pytcolbert.index_scorer(query_encoded=True, add_ranks=True, batch_size=5000) %1000)
            )
        else:
            prf_pipe = (
                dense_e2e  
                >> ColbertPRF(pytcolbert, k=k, fb_docs=fb_docs, fb_embs=fb_embs, beta=beta, return_docs=False)
                >> pytcolbert.set_retrieve(query_encoded=True)
                >> (pytcolbert.index_scorer(query_encoded=True, add_ranks=True, batch_size=5000) % 1000)
            )
        return prf_pipe

    def explain_doc(self, query : str, doc : Union[str,int]):
        """
        Provides a diagram explaining the interaction between a query and a given docno
        """
        if isinstance(doc,str):
            pid = self.docno2docid(doc)
        elif isinstance(doc,int):
            pid = doc
        else:
            raise ValueError("Expected docno(str) or docid(int)")
        embsD = self._rrm().get_embedding(pid)
        idsD = self.nn_term().get_tokens_for_doc(pid)
        return self._explain(query, embsD, idsD)

    def explain_text(self, query : str, document : str):
        """
        Provides a diagram explaining the interaction between a query and the text of a document
        """
        embsD, idsD = self.args.inference.docFromText([document], with_ids=True)
        return self._explain(query, embsD, idsD)
    
    def _explain(self, query, embsD, idsD):
        embsQ, idsQ, masksQ = self.args.inference.queryFromText([query], with_ids=True)

        interaction = (embsQ[0] @ embsD[0].T).cpu().numpy().T
        
        import numpy as np
        import matplotlib.pyplot as plt
        from matplotlib.gridspec import GridSpec

        tokenmap = {"[unused1]" : "[D]", "[unused0]" : "[Q]"}

        fig = plt.figure(figsize=(4, 12)) 
        gs = GridSpec(2, 1, height_ratios=[1, 20]) 

        ax1=fig.add_subplot(gs[0])
        ax2=fig.add_subplot(gs[1])
        
        ax2.matshow(interaction, cmap=plt.cm.Blues)
        qtokens = self.args.inference.query_tokenizer.tok.convert_ids_to_tokens(idsQ[0])
        dtokens = self.args.inference.query_tokenizer.tok.convert_ids_to_tokens(idsD[0])
        qtokens = [tokenmap[t] if t in tokenmap else t for t in qtokens]
        dtokens = [tokenmap[t] if t in tokenmap else t for t in dtokens]

        ax2.set_xticks(range(32), minor=False)
        ax2.set_xticklabels(qtokens, rotation=90)
        ax2.set_yticks(range(len(idsD[0])))
        ax2.set_yticklabels(dtokens)
        ax2.set_anchor("N")

        contributions=[]
        for i in range(32):
            maxpos = np.argmax(interaction[:,i])
            plt.text(i-0.25, maxpos+0.1, "X", fontsize=5)
            contributions.append(interaction[maxpos,i])

        from sklearn.preprocessing import minmax_scale
        ax1.bar([0.5 + i for i in range(0,32)], contributions, color=plt.cm.Blues(minmax_scale(contributions, feature_range=(0.4, 1))))
        ax1.set_xlim([0,32])
        ax1.set_xticklabels([])
        fig.tight_layout()
        #fig.subplots_adjust(hspace=-0.37)
        return fig

from pyterrier.transformer import TransformerBase
import pandas as pd

class ColbertPRF(TransformerBase):
    def __init__(self, pytcfactory, k, fb_embs, beta=1, r = 42, return_docs = False, fb_docs=10, verbose=False,  *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.k = k
        self.fb_embs = fb_embs
        self.beta = beta
        self.return_docs = return_docs
        self.fb_docs = fb_docs
        self.pytcfactory = pytcfactory
        self.fnt = pytcfactory.nn_term(df=True)
        self.r = r
        self.verbose = verbose
        import torch
        import numpy as np
        num_docs = self.fnt.num_docs
        self.idfdict = {}
        for tid in pt.tqdm(range(self.fnt.inference.query_tokenizer.tok.vocab_size)):
            df = self.fnt.getDF_by_id(tid)
            idfscore = np.log((1.0+num_docs)/(df+1))
            self.idfdict[tid] = idfscore
        assert self.k > self.fb_embs ,"fb_embs should be smaller than number of clusters"
        self._init_clustering()

    def _init_clustering(self):
        import sklearn
        from packaging.version import Version
        from warnings import warn
        if Version(sklearn.__version__) > Version('0.23.2'):
            warn("You have sklearn version %s - sklearn KMeans clustering changed in 0.24, so performance may differ from those reported in the ICTIR 2021 paper, which used 0.23.2. "
            "See also https://github.com/scikit-learn/scikit-learn/issues/19990" % str(sklearn.__version__))

    def _get_centroids(self, prf_embs):
        from sklearn.cluster import KMeans
        kmn = KMeans(self.k, random_state=self.r)
        kmn.fit(prf_embs)
        return np.float32(kmn.cluster_centers_)
    
    def _get_prf_embs(self, df, num_docs):
        return torch.cat([self.pytcfactory.rrm.get_embedding(docid) for docid in df.head(num_docs).docid.values])
        
    def transform_query(self, topic_and_res : pd.DataFrame) -> pd.DataFrame:
        topic_and_res = topic_and_res.sort_values('rank')
        prf_embs = self._get_prf_embs(topic_and_res, self.fb_docs)
        
        # perform clustering on the document embeddings to identify the representative embeddings
        centroids = self._get_centroids(prf_embs)
        
        # get the most likely tokens for each centroid        
        toks2freqs = self.fnt.get_nearest_tokens_for_embs(centroids)

        # rank the clusters by descending idf
        emb_and_score = []
        for cluster, tok2freq in zip(range(self.k),toks2freqs):
            if len(tok2freq) == 0:
                continue
            most_likely_tok = max(tok2freq, key=tok2freq.get)
            tid = self.fnt.inference.query_tokenizer.tok.convert_tokens_to_ids(most_likely_tok)
            emb_and_score.append( (centroids[cluster], most_likely_tok, tid, self.idfdict[tid]) ) 
        sorted_by_second = sorted(emb_and_score, key=lambda tup: -tup[3])
        

       # build up the new dataframe columns
        toks=[]
        scores=[]
        exp_embds = []
        for i in range(min(self.fb_embs, len(sorted_by_second))):
            emb, tok, tid, score = sorted_by_second[i]
            toks.append(tok)
            scores.append(score)
            exp_embds.append(emb)
        
        first_row = topic_and_res.iloc[0]
        
        # concatenate the new embeddings to the existing query embeddings 
        newemb = torch.cat([
            first_row.query_embs, 
            torch.Tensor(exp_embds)])
        
        # the weights column defines important of each query embedding
        weights = torch.cat([ 
            torch.ones(len(first_row.query_embs)),
            self.beta * torch.Tensor(scores)]
        )
        
        # generate the revised query dataframe row
        rtr = pd.DataFrame([
            [first_row.qid, 
             first_row.docno,
             first_row.query, 
             newemb, 
             toks, 
             weights ]
            ],
            columns=["qid","docno", "query", "query_embs", "query_toks", "query_weights"])
        return rtr

    def transform(self, topics_and_docs : pd.DataFrame) -> pd.DataFrame:
        # validation of the input
        required = ["qid", "query", "docno", "query_embs", "rank"]
        for col in required:
            assert col in topics_and_docs.columns
        
        #restore the docid column if missing
        if "docid" not in topics_and_docs:
            topics_and_docs = self.pytcfactory.add_docids(topics_and_docs)
        
        rtr = []
        it = topics_and_docs.groupby("qid")
        if self.verbose:
            it = pt.tqdm(it, unit='query', desc='colbert-prf')
        for qid, res in it:
            new_query_df = self.transform_query(res)     
            if self.return_docs:
                return_doc_rows = ["qid", "docno", "docid"]
                if 'doc_embs' in topics_and_docs.columns:
                    return_doc_rows.append('doc_embs')
                new_query_df = res[return_doc_rows].merge(new_query_df, on=["qid"])                
                new_query_df = new_query_df.rename(columns={'docno_x':'docno'})
            rtr.append(new_query_df)
        return pd.concat(rtr)


class MultiFaissMmapIndex:
    """
    This is a replacement for colbert's FaissIndex that:
     - Supports multiple index files
     - Reads them as mmap'd files, rather than loading them all into memory
    which are useful when the index would be too large to fit into memory as a single index.
    """
    def __init__(self, base_path, nprobe):
        print_message("#> Loading the FAISS indexes from", base_path, "..")

        self.faiss_indices = []
        self.faiss_indices_offsets = [0]
        for faiss_index_path in sorted(glob(f'{base_path}/*.faiss'), key=lambda x: int(x.split('/')[-1].split('.')[0])):
            index = faiss.read_index(faiss_index_path, faiss.IO_FLAG_MMAP)
            index.nprobe = nprobe
            self.faiss_indices.append(index)
            self.faiss_indices_offsets.append(self.faiss_indices_offsets[-1] + index.ntotal)

        self.doc_offsets = np.memmap(f'{base_path}/doclens.psum.np', dtype=np.uint64, mode='r')

    def retrieve(self, faiss_depth, Q, verbose=False):
        embedding_ids = self.queries_to_embedding_ids(faiss_depth, Q, verbose=verbose)
        return self.embedding_ids_to_pids(embedding_ids, verbose=verbose)

    def queries_to_embedding_ids(self, faiss_depth, Q, verbose=True):
        # Flatten into a matrix for the faiss search.
        num_queries, embeddings_per_query, dim = Q.size()
        Q_faiss = Q.view(num_queries * embeddings_per_query, dim).cpu().contiguous()

        # Search in large batches with faiss.
        print_message("#> Search in batches with faiss. \t\t",
                      f"Q.size() = {Q.size()}, Q_faiss.size() = {Q_faiss.size()}",
                      condition=verbose)

        embeddings_ids = []
        faiss_bsize = embeddings_per_query * 5000
        per_index_faiss_depth = faiss_depth // len(self.faiss_indices)
        for offset in range(0, Q_faiss.size(0), faiss_bsize):
            endpos = min(offset + faiss_bsize, Q_faiss.size(0))

            print_message("#> Searching from {} to {}...".format(offset, endpos), condition=verbose)

            some_Q_faiss = Q_faiss[offset:endpos].float().numpy()
            for i, (index, id_offset) in enumerate(zip(self.faiss_indices, self.faiss_indices_offsets)):
                print_message(f"#> Searching index {i}...", condition=verbose)
                _, some_embedding_ids = index.search(some_Q_faiss, per_index_faiss_depth)
                embeddings_ids.append(some_embedding_ids + id_offset)

        embeddings_ids = np.concatenate(embeddings_ids)

        # Reshape to (number of queries, non-unique embedding IDs per query)
        embeddings_ids = embeddings_ids.reshape(num_queries, -1)

        return embeddings_ids

    def search(self, embs, k, verbose=False):
        scores, embeddings_ids = [], []
        per_index_faiss_depth = k // len(self.faiss_indices)
        for i, (index, id_offset) in enumerate(zip(self.faiss_indices, self.faiss_indices_offsets)):
            print_message(f"#> Searching index {i}...", condition=verbose)
            some_scores, some_embedding_ids = index.search(embs, per_index_faiss_depth)
            scores.append(some_scores)
            embeddings_ids.append(some_embedding_ids + id_offset)
        scores = np.concatenate(scores, axis=1)
        embeddings_ids = np.concatenate(embeddings_ids, axis=1)
        return scores, embeddings_ids


    def embedding_ids_to_pids(self, embedding_ids, verbose=True):
        # Find unique PIDs per query.
        print_message("#> Lookup the PIDs..", condition=verbose)
        all_pids = np.searchsorted(self.doc_offsets, embedding_ids, side='right') - 1

        print_message(f"#> Converting to a list [shape = {all_pids.shape}]..", condition=verbose)
        all_pids = all_pids.tolist()

        print_message("#> Removing duplicates..", condition=verbose)
        all_pids = [list(set(pids)) for pids in all_pids]

        return all_pids
