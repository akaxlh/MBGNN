import numpy as np
from Params import args
import Utils.TimeLogger as logger
from Utils.TimeLogger import log
import Utils.NNLayers as NNs
from Utils.NNLayers import FC, Regularize, Activate, Dropout, Bias, getParam, defineParam
from DataHandler import DataHandler, negSamp, transpose, transToLsts, loadPretrnEmbeds
import tensorflow as tf
from tensorflow.core.protobuf import config_pb2
import pickle
import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

class Recommender:
	def __init__(self, sess, handler):
		self.sess = sess
		self.handler = handler

		self.pTrnUsr, self.pTrnItm = loadPretrnEmbeds()
		args.user, args.item = self.handler.trnMats[0].shape
		print('USER', args.user, 'ITEM', args.item)
		assert self.pTrnUsr.shape[0] == args.user and self.pTrnItm.shape[0] == args.item, str(self.pTrnUsr.shape[0])
		self.metrics = dict()
		mets = ['Loss', 'preLoss', 'HR', 'NDCG']
		for met in mets:
			self.metrics['Train'+met] = list()
			self.metrics['Test'+met] = list()

	def makePrint(self, name, ep, reses, save):
		ret = 'Epoch %d/%d, %s: ' % (ep, args.epoch, name)
		for metric in reses:
			val = reses[metric]
			ret += '%s = %.4f, ' % (metric, val)
			tem = name + metric
			if save and tem in self.metrics:
				self.metrics[tem].append(val)
		ret = ret[:-2] + '  '
		return ret

	def run(self):
		self.prepareModel()
		log('Model Prepared')
		if args.load_model != None:
			self.loadModel()
			stloc = len(self.metrics['TrainLoss'])
		else:
			stloc = 0
			init = tf.global_variables_initializer()
			self.sess.run(init)
			log('Variables Inited')
		for ep in range(stloc, args.epoch):
			test = (ep % args.test_epoch == 0)
			reses = self.trainEpoch()
			log(self.makePrint('Train', ep, reses, test))
			if test:
				reses = self.testEpoch()
				log(self.makePrint('Test', ep, reses, test))
				self.saveHistory()
			print()
		reses = self.testEpoch()
		log(self.makePrint('Test', args.epoch, reses, True))
		self.saveHistory()

	def transMsg(self, feature, multMats):
		catlat1 = []
		paramId = 'dfltP%d' % NNs.getParamId()
		for inp in multMats:
			temlat = tf.sparse.sparse_dense_matmul(inp, feature)
			# memo att for context learning
			memoatt = FC(temlat, args.memosize, activation='relu', reg=True, useBias=True)
			memoTrans = tf.reshape(FC(memoatt, args.latdim**2, reg=True, name=paramId, reuse=True), [-1, args.latdim, args.latdim])
			transLat = tf.reduce_sum(tf.reshape(temlat, [-1, args.latdim, 1]) * memoTrans, axis=1)
			catlat1.append(transLat)
			self.translats.append(transLat)
		# catlat2 = catlat1
		catlat2 = NNs.selfAttention(catlat1, number=args.behNum, inpDim=args.latdim, numHeads=args.attHead)
		# aggregation gate
		weights = []
		paramId = 'dfltP%d' % NNs.getParamId()
		for catlat in catlat2:
			temlat = FC(catlat, args.latdim//2, useBias=True, reg=True, activation='relu', name=paramId+'_1', reuse=True)
			weight = FC(temlat, 1, useBias=True, reg=True, name=paramId+'_2', reuse=True)
			weights.append(weight)
		stkWeight = tf.concat(weights, axis=1)
		sftWeight = tf.reshape(tf.nn.softmax(stkWeight, axis=1), [-1, args.behNum, 1])
		stkCatlat = tf.stack(catlat2, axis=1)
		lat = tf.reshape(tf.reduce_sum(sftWeight * stkCatlat, axis=1), [-1, args.latdim])
		for i in range(0):
			lat = FC(lat, args.latdim, reg=True, useBias=True, activation='relu') + lat
		return lat

	def ours(self):
		self.translats = list()
		ulats = [self.uEmbed0]
		ilats = [self.iEmbed0]
		for i in range(args.gnn_layer):
			ulat = self.transMsg(ilats[-1], self.uiMats)
			ilat = self.transMsg(ulats[-1], self.iuMats)
			ulats.append(ulat)
			ilats.append(ilat)

		for i in range(args.gnn_layer+1):
			ulats[i] = ulats[i] / (1e-6+tf.sqrt(1e-6+tf.reduce_sum(tf.square(ulats[i]), axis=-1, keepdims=True)))
			ilats[i] = ilats[i] / (1e-6+tf.sqrt(1e-6+tf.reduce_sum(tf.square(ilats[i]), axis=-1, keepdims=True)))

		latnum = len(ulats)
		ulats = tf.stack(ulats, axis=1)
		ilats = tf.stack(ilats, axis=1)

		pckULats = tf.reshape(tf.nn.embedding_lookup(ulats, self.uids), [-1, args.latdim])
		pckILats = tf.reshape(tf.nn.embedding_lookup(ilats, self.iids), [-1, args.latdim])
		ukeys = tf.reshape(FC(pckULats, args.latdim, reg=True, name='key', reuse=True), [-1, latnum, 1, args.attHead, args.latdim//args.attHead])
		ikeys = tf.reshape(FC(pckILats, args.latdim, reg=True, name='key', reuse=True), [-1, 1, latnum, args.attHead, args.latdim//args.attHead])
		uvals = tf.reshape(FC(pckULats, args.latdim, reg=True, name='val', reuse=True), [-1, latnum, 1, args.attHead, args.latdim//args.attHead])
		ivals = tf.reshape(FC(pckILats, args.latdim, reg=True, name='val', reuse=True), [-1, 1, latnum, args.attHead, args.latdim//args.attHead])

		att = Activate(tf.reduce_sum(ukeys * ikeys, axis=-1, keepdims=True), 'relu')
		self.att = tf.squeeze(att)

		lat = uvals * ivals
		predLat = tf.reshape(tf.reduce_sum(att * lat, axis=[1, 2]), [-1, args.latdim]) * args.mult

		for i in range(1):
			predLat = FC(predLat, args.latdim, reg=True, useBias=True, activation='relu') + predLat
		pred = tf.squeeze(FC(predLat, 1, reg=True, useBias=True))
		return pred

	def prepareModel(self):
		self.uiMats = []
		self.iuMats = []
		for i in range(args.behNum):
			self.uiMats.append(tf.sparse_placeholder(tf.float32, name='ui'+str(i)))
			self.iuMats.append(tf.sparse_placeholder(tf.float32, name='iu'+str(i)))
		self.uEmbed0 = tf.placeholder(name='uEmbed0', dtype=tf.float32, shape=[None, args.latdim])
		self.iEmbed0 = tf.placeholder(name='iEmbed0', dtype=tf.float32, shape=[None, args.latdim])
		self.uids = tf.placeholder(name='uids', dtype=tf.int32, shape=[None])
		self.iids = tf.placeholder(name='iids', dtype=tf.int32, shape=[None])

		self.pred = self.ours()
		sampNum = tf.shape(self.iids)[0]//2
		posPred = tf.slice(self.pred, [0], [sampNum])
		negPred = tf.slice(self.pred, [sampNum], [-1])
		self.preLoss = tf.reduce_sum(tf.maximum(0.0, 1.0 - (posPred - negPred))) / args.batch
		self.regLoss = args.reg * Regularize()
		self.loss = self.preLoss + self.regLoss

		globalStep = tf.Variable(0, trainable=False)
		learningRate = tf.train.exponential_decay(args.lr, globalStep, args.decay_step, args.decay, staircase=True)
		self.optimizer = tf.train.AdamOptimizer(learningRate).minimize(self.loss, global_step=globalStep)

	def sampleTrainBatch(self, batchIds, itmnum, label):
		temLabel = label[batchIds].toarray()
		batch = len(batchIds)
		temlen = batch * 2 * args.sampNum
		uIntLoc = [None] * temlen
		iIntLoc = [None] * temlen
		cur = 0
		for i in range(batch):
			posset = np.reshape(np.argwhere(temLabel[i]!=0), [-1])
			if len(posset) == 0:
				poslocs = list(np.random.choice(args.item, args.sampNum))
				neglocs = poslocs
			else:
				poslocs = np.random.choice(posset, args.sampNum)
				neglocs = negSamp(temLabel[i], args.sampNum, itmnum)
			for j in range(args.sampNum):
				uIntLoc[cur] = uIntLoc[cur+temlen//2] = batchIds[i]
				iIntLoc[cur] = poslocs[j]
				iIntLoc[cur+temlen//2] = neglocs[j]
				cur += 1
		return uIntLoc, iIntLoc

	def trainEpoch(self):
		num = args.user
		sfIds = np.random.permutation(num)[:args.trnNum]
		epochLoss, epochPreLoss = [0] * 2
		num = len(sfIds)
		steps = int(np.ceil(num / args.batch))

		pckAdjs, pckTpAdjs, usrs, itms = self.handler.sampleLargeGraph(sfIds)
		pckLabel = transpose(transpose(self.handler.trnLabel[usrs])[itms])
		usrIdMap = dict(map(lambda x: (usrs[x], x), range(len(usrs))))
		sfIds = list(map(lambda x: usrIdMap[x], sfIds))
		feeddict = {self.uEmbed0: self.pTrnUsr[usrs], self.iEmbed0: self.pTrnItm[itms]}
		for i in range(args.behNum):
			feeddict[self.uiMats[i]] = transToLsts(pckAdjs[i])
			feeddict[self.iuMats[i]] = transToLsts(pckTpAdjs[i])

		for i in range(steps):
			st = i * args.batch
			ed = min((i+1) * args.batch, num)
			batchIds = sfIds[st: ed]

			uIntLoc, iIntLoc = self.sampleTrainBatch(batchIds, pckAdjs[0].shape[1], pckLabel)
			target = [self.optimizer, self.preLoss, self.regLoss, self.loss]
			feeddict[self.uids] = uIntLoc
			feeddict[self.iids] = iIntLoc
			res = self.sess.run(target, feed_dict=feeddict, options=config_pb2.RunOptions(report_tensor_allocations_upon_oom=True))
			preLoss, regLoss, loss = res[1:]

			epochLoss += loss
			epochPreLoss += preLoss
			log('Step %d/%d: loss = %.2f, regLoss = %.2f          ' % (i, steps, loss, regLoss), save=False, oneline=True)
		ret = dict()
		ret['Loss'] = epochLoss / steps
		ret['preLoss'] = epochPreLoss / steps
		return ret

	def sampleTestBatch(self, batchIds, label, tstInt):
		batch = len(batchIds)
		temTst = tstInt[batchIds]
		temLabel = label[batchIds].toarray()
		temlen = batch * 100
		uIntLoc = [None] * temlen
		iIntLoc = [None] * temlen
		tstLocs = [None] * batch
		cur = 0
		for i in range(batch):
			posloc = temTst[i]
			negset = np.reshape(np.argwhere(temLabel[i]==0), [-1])
			rdnNegSet = np.random.permutation(negset)[:99]
			locset = np.concatenate((rdnNegSet, np.array([posloc])))
			tstLocs[i] = locset
			for j in range(100):
				uIntLoc[cur] = batchIds[i]
				iIntLoc[cur] = locset[j]
				cur += 1
		return uIntLoc, iIntLoc, temTst, tstLocs

	def testEpoch(self):
		epochHit, epochNdcg = [0] * 2
		ids = self.handler.tstUsrs
		num = len(ids)
		testbatch = args.batch
		steps = int(np.ceil(num / testbatch))

		posItms = self.handler.tstInt[ids]
		pckAdjs, pckTpAdjs, usrs, itms = self.handler.sampleLargeGraph(ids, list(set(posItms)))
		pckLabel = transpose(transpose(self.handler.trnLabel[usrs])[itms])
		usrIdMap = dict(map(lambda x: (usrs[x], x), range(len(usrs))))
		itmIdMap = dict(map(lambda x: (itms[x], x), range(len(itms))))
		ids = list(map(lambda x: usrIdMap[x], ids))
		itmMapping = (lambda x: None if (x is None) or x not in itmIdMap else itmIdMap[x])
		pckTstInt = np.array(list(map(lambda x: itmMapping(self.handler.tstInt[usrs[x]]), range(len(usrs)))))
		feeddict = {self.uEmbed0: self.pTrnUsr[usrs], self.iEmbed0: self.pTrnItm[itms]}
		for i in range(args.behNum):
			feeddict[self.uiMats[i]] = transToLsts(pckAdjs[i])
			feeddict[self.iuMats[i]] = transToLsts(pckTpAdjs[i])

		for i in range(steps):
			st = i * testbatch
			ed = min((i+1) * testbatch, num)
			batchIds = ids[st: ed]
			uIntLoc, iIntLoc, temTst, tstLocs = self.sampleTestBatch(batchIds, pckLabel, pckTstInt)
			feeddict[self.uids] = uIntLoc
			feeddict[self.iids] = iIntLoc
			preds = self.sess.run(self.pred, feed_dict=feeddict, options=config_pb2.RunOptions(report_tensor_allocations_upon_oom=True))
			hit, ndcg = self.calcRes(np.reshape(preds, [ed-st, 100]), temTst, tstLocs)
			epochHit += hit
			epochNdcg += ndcg
			log('Step %d/%d: hit = %d, ndcg = %.2f          ' % (i, steps, hit, ndcg), save=False, oneline=True)
		ret = dict()
		ret['HR'] = epochHit / num
		ret['NDCG'] = epochNdcg / num
		return ret

	def calcRes(self, preds, temTst, tstLocs):
		hit = 0
		ndcg = 0
		for j in range(preds.shape[0]):
			predvals = list(zip(preds[j], tstLocs[j]))
			predvals.sort(key=lambda x: x[0], reverse=True)
			shoot = list(map(lambda x: x[1], predvals[:args.shoot]))
			if temTst[j] in shoot:
				hit += 1
				ndcg += np.reciprocal(np.log2(shoot.index(temTst[j])+2))
		return hit, ndcg

	def saveHistory(self):
		if args.epoch == 0:
			return
		with open('History/' + args.save_path + '.his', 'wb') as fs:
			pickle.dump(self.metrics, fs)

		saver = tf.train.Saver()
		saver.save(self.sess, 'Models/' + args.save_path)
		log('Model Saved: %s' % args.save_path)

	def loadModel(self):
		saver = tf.train.Saver()
		saver.restore(sess, 'Models/' + args.load_model)
		with open('History/' + args.load_model + '.his', 'rb') as fs:
		    self.metrics = pickle.load(fs)
		log('Model Loaded')

if __name__ == '__main__':
	logger.saveDefault = True
	config = tf.ConfigProto()
	config.gpu_options.allow_growth = True

	log('Start')
	handler = DataHandler()
	handler.LoadData()
	log('Load Data')

	with tf.Session(config=config) as sess:
		recom = Recommender(sess, handler)
		recom.run()